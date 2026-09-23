"""`_webrunner_shared.run_batch` 的**退出碼**與**降級路徑**。

`run_batch` 是兩個變體共用的批次主迴圈，252 行敘述。既有的
`test_webrunner_shared.py` 用 `_RunBatchHarness` 把接線（`end` 標記、零產出、
padding pop、resume）測得很完整，但 2026-09-20 量出來仍有 **47 行從來沒有被執行
過**，而那 47 行幾乎全部是**出事時才走的那一半**：

* 五條「這一對跳過」的路（主提示詞、負面提示詞、Character 2 開關、char1、char2
  任何一格填不進去）；
* 週期性的 Chrome 重啟（記憶體回收）與它後面那三行 `prev_* = None`；
* 三個終止碼：setup 失敗的 2、生成被站方擋住的 4、零產出的 3；
* 電源要求拿不到／只拿到舊版旗標的兩行診斷；
* resume 檢查點的兩個邊界（磁碟上還沒有圖、已經做完但沒 pop）。

這一檔補的就是那一半。核心是一條**推導出來的不變式**，而不是一條路寫一句斷言。

## 為什麼退出碼是這支函式最重要的輸出

`run_batch` 的 rc 是**監督者唯一的輸入**，而每一個值對應一個不同的動作：`0` ＝
佇列做完了，不要重生；`3` ＝這一輪零產出，重生但要計數、連續太多次就放棄；
`4` ＝站方擋住生成，乾淨停止**不要**重生；其餘（例外炸穿成 `1`）＝無限重生。
所以「rc 講的事」與「事件檔講的事」**必須是同一件事**：`todo_done` 是給人看的
「做完了」，`critical_error` 是「壞了」。兩者對不起來時，症狀不是當場出錯，而是
監督者做出一個看起來合理、其實相反的決定——這正是最難從現場推回原因的失效。

`test_the_exit_code_and_the_event_always_agree` 對**每一條**終止路徑跑同一條檢查。
與 `VERIFY-SETUP` 的 rc／結果行契約
（單圖的 `single_image_done` 一則契約）是同一個做法。

## 電源要求：`finally` 那一行的覆蓋範圍

`_awake.release()` 在 `finally` 裡，註解寫著「每一條離開路徑都要放掉：正常結束、
rc=3／rc=4、以及往上炸的例外」。那是一句**宣稱**，而在此之前沒有任何東西在檢查
它——`finally` 底下多一個 `return`、或有人把 `release()` 搬進 `try` 的尾段，行為
測試全部照綠。這裡把那句宣稱變成一條跟終止路徑表共用語料的斷言。
"""
import pytest

import _webrunner_shared as ws
from test_webrunner_shared import FakeBrowserPort, _RunBatchHarness


# ---------------------------------------------------------------------------
# 共用工具
# ---------------------------------------------------------------------------

class _RecordingAwake:
    """`StayAwake` 的替身；記錄 acquire 回了什麼、release 被叫幾次。"""

    instances: list = []

    def __init__(self, got="power-request"):
        self.got = got
        self.releases = 0
        _RecordingAwake.instances.append(self)

    def acquire(self):
        return self.got

    def release(self):
        self.releases += 1


def _use_awake(h, got="power-request"):
    _RecordingAwake.instances = []
    h.patch("StayAwake", lambda: _RecordingAwake(got))


class _RestartablePort(FakeBrowserPort):
    """`FakeBrowserPort` 沒有 `restart()`——週期性重啟那一段走不到它。"""

    def __init__(self):
        super().__init__()
        self.restarts = []

    def restart(self, email, password):
        self.restarts.append((email, password))


def _run(port, *, setup_ok=True, **kw):
    return ws.run_batch(port, "email", "pw",
                        setup_fn=lambda: setup_ok,
                        minimize_fn=lambda: None, **kw)


def _one_pair(h, prompts=("P1",), chars=("a",)):
    h.write_queue("todo_prompt.md", list(prompts))
    h.write_queue("todo_character1.md", list(chars))


# ---------------------------------------------------------------------------
# 不變式：退出碼與事件講的是同一件事
# ---------------------------------------------------------------------------

def _terminal_clean(h):
    _one_pair(h)


def _terminal_end_sentinel(h):
    _one_pair(h, prompts=("end", "P2"), chars=("a", "b"))


def _terminal_setup_failed(h):
    _one_pair(h)


def _terminal_zero_save(h):
    h.gen_saved = 0
    _one_pair(h)


def _terminal_blocked(h):
    _one_pair(h)

    def _boom(*_a, **_k):
        raise ws.GenerationBlockedError("站方擋住了")
    h.patch("generate_loop", _boom)


def _terminal_crash_after_a_barren_character(h):
    """第一個角色「跑完但一張都沒存」，第二個角色炸掉。

    這是 `produced > 0 and total_saved == 0` 那條路唯一到得了的形狀：`produced`
    在 `generate_loop` **回來之後**才 +1，所以第一個角色就炸的話 `produced` 還是
    0，例外會直接往上炸（那是刻意的——沒跑過任何一個角色就死掉不算「零產出的一
    輪」）。
    """
    _one_pair(h, prompts=("P1", "P2"), chars=("a", "b"))
    calls = []

    def _gen(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            return 0
        raise RuntimeError("chrome went away")
    h.patch("generate_loop", _gen)


# (名稱, 佈置, 預期 rc, 預期事件)。`None` ＝這條路不該發任何事件。
#
# 往上炸的那一條**不在表裡**：它沒有 rc 可以比，而且它的重點是「不可以被
# 降級成 rc=3」，語料跟這裡的其他筆不一樣。它自己一支。
_TERMINALS = [
    ("clean finish", _terminal_clean, 0, "todo_done"),
    ("end sentinel", _terminal_end_sentinel, 0, "todo_done"),
    ("setup failed", _terminal_setup_failed, 2, None),
    ("zero save backstop", _terminal_zero_save, 3, "critical_error"),
    ("generation blocked", _terminal_blocked, 4, "generation_blocked"),
    ("crash after a barren character",
     _terminal_crash_after_a_barren_character, 3, "critical_error"),
]
_TERMINAL_IDS = [case[0] for case in _TERMINALS]


@pytest.mark.parametrize("name, setup, rc, event", _TERMINALS,
                         ids=_TERMINAL_IDS)
def test_the_exit_code_and_the_event_always_agree(name, setup, rc, event):
    """rc 是監督者唯一的輸入，事件檔是人唯一看得到的說明——兩者不可以講不同的事。

    最要緊的一條是 `todo_done` **只**在 rc 0 時出現：監督者靠 rc 決定重不重生，
    人靠 `todo_done` 認定「佇列做完了」。一條失敗路徑若順手發了 `todo_done`，
    畫面上會顯示這一輪正常結束，而監督者其實正在重生——兩邊都不會報錯。
    """
    with _RunBatchHarness() as h:
        setup(h)
        got = _run(FakeBrowserPort(), setup_ok=(name != "setup failed"))
        events = [et for et, _kw in h.events]
    assert got == rc, f"{name}: rc 應該是 {rc}，實際 {got}"
    if event is not None:
        assert event in events, f"{name}: 少了 {event} 事件（有的是 {events}）"
    # 這一半是承重的：`todo_done` 是「佇列做完了」的唯一宣告。
    assert ("todo_done" in events) is (got == 0), (
        f"{name}: rc={got} 卻 {'發了' if 'todo_done' in events else '沒發'} "
        "`todo_done`——監督者與人看到的結論不一致")


def test_a_crash_after_saving_images_is_not_downgraded_to_zero_progress():
    """有存到圖的崩潰要往上炸（rc=1，監督者無限重生），不可以混進 rc=3。

    rc=3 的語意是「這一輪零產出」，監督者會**計數**並在連續太多次之後放棄。把一次
    偶發的崩潰講成零產出，等於讓一個其實在正常產圖的批次慢慢走向「放棄」。
    """
    with _RunBatchHarness() as h:
        _one_pair(h, prompts=("P1", "P2"), chars=("a", "b"))
        calls = []

        def _gen(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                return 999
            raise RuntimeError("chrome went away")
        h.patch("generate_loop", _gen)
        with pytest.raises(RuntimeError):
            _run(FakeBrowserPort())
        assert len(calls) == 2, "第二個角色沒跑到，這一支沒測到它想測的東西"


@pytest.mark.parametrize("name, setup, rc, event", _TERMINALS,
                         ids=_TERMINAL_IDS)
def test_the_power_request_is_released_on_every_exit_path(
        name, setup, rc, event):
    """`finally` 裡那一行的宣稱是「每一條離開路徑都要放掉」——逐條量。

    沒有這一支的話，把 `release()` 從 `finally` 搬到 `try` 的尾段（很自然的一次
    「整理」）只會讓失敗路徑漏放，而**漏放沒有任何症狀**：電源要求會一直生效到
    行程結束，而這個行程本來就是長命的。
    """
    with _RunBatchHarness() as h:
        _use_awake(h)
        setup(h)
        _run(FakeBrowserPort(), setup_ok=(name != "setup failed"))
    assert len(_RecordingAwake.instances) == 1, "電源要求不是只建一次"
    assert _RecordingAwake.instances[0].releases == 1, (
        f"{name}：離開時沒有剛好放掉一次電源要求")


def test_the_power_request_is_released_even_when_the_exception_escapes():
    """往上炸的那一條也要放掉——而它是唯一一條 `return` 碰不到的路徑。"""
    with _RunBatchHarness() as h:
        _use_awake(h)
        _one_pair(h)
        h.patch("generate_loop", _raise_runtime)
        with pytest.raises(RuntimeError):
            _run(FakeBrowserPort())
    assert _RecordingAwake.instances[0].releases == 1


def _raise_runtime(*_a, **_k):
    raise RuntimeError("chrome went away")


@pytest.mark.parametrize("got, token", [
    ("power-request", "power-request"),
    ("execution-state", "execution-state"),
    (None, "都拿不到"),
])
def test_each_power_outcome_says_which_one_it_got(capsys, got, token):
    """三條分支要分得出來，而且**印出那個 token 的字面值**。

    理由寫在那段程式碼自己的註解裡：中文敘述會隨著理解被重寫（那一段 2026-09-20
    就重寫過一次），而 `power-request`／`execution-state` 這兩個 token 是程式真正
    的判斷依據——事後 grep log 才問得出「那一輪到底拿到了哪一個」。只拿到舊版旗標
    的機器（這台就是 Modern Standby）保不住行程，跟拿到了完全是兩件事。
    """
    with _RunBatchHarness() as h:
        _use_awake(h, got)
        _one_pair(h)
        _run(FakeBrowserPort())
    out = capsys.readouterr().out
    power = [line for line in out.splitlines() if "[power]" in line]
    assert len(power) == 1, f"電源那一段印了 {len(power)} 行：{power}"
    assert token in power[0], power[0]


def test_the_power_request_is_skipped_when_the_config_turns_it_off():
    """反面對照：設定關掉時整段不跑，一行都不印。

    少了它，把 `if load_batch_config().get("keep_system_awake", True)` 改成
    無條件執行也會通過上面那三支。
    """
    with _RunBatchHarness() as h:
        _use_awake(h)
        h.cfg_over = {"keep_system_awake": False}
        _one_pair(h)
        _run(FakeBrowserPort())
    assert _RecordingAwake.instances[0].releases == 1, (
        "沒有取得也要放掉——`release()` 是冪等的，而漏放才是危險的那一邊")


# ---------------------------------------------------------------------------
# 「這一對填不進去」的五條降級路
# ---------------------------------------------------------------------------

# (`with_retry` 的標籤, 印出來的那句話, 傳給崩潰檢查的位置名)
#
# 第三欄明列，不要從第一欄推：`fill_main_undesired` 那一步傳出去的是
# `fill_undesired (…)`，兩者**不一樣**。第一版用字串前綴猜，於是那一筆紅了——
# 而真正危險的是反過來猜對：一個放寬到永遠成立的前綴會讓這一欄形同不存在。
_SKIP_STEPS = [
    ("fill_main_prompt", "main prompt replacement failed", "fill_main_prompt"),
    ("fill_main_undesired", "undesired replacement failed", "fill_undesired"),
    ("set_character2_state", "Character 2 UI state could not be updated",
     "set_character2_state"),
    ("fill_char1", "char1 fill failed", "fill_char1"),
    ("fill_char2", "char2 replacement failed", "fill_char2"),
]


def _fail_only(h, failing: str):
    """讓 `with_retry` 對指定的那一步回 False，其餘照常。"""
    real = ws.with_retry

    def _fake(label, func, *a, **k):
        if label == failing:
            return False
        return real(label, func, *a, **k)
    h.patch("with_retry", _fake)


@pytest.mark.parametrize("step, message, where", _SKIP_STEPS,
                         ids=[s for s, _m, _w in _SKIP_STEPS])
def test_a_field_that_cannot_be_filled_skips_the_pair_without_consuming_it(
        capsys, step, message, where):
    """填不進去就跳過這一對，而且**不可以把佇列條目吃掉**。

    這是整支函式裡最容易造成安靜損失的一族：五格任何一格填不進去，都代表那一對
    的畫面狀態**不是**它該有的樣子。硬著頭皮產下去會得到一批「上一個角色的特徵
    混進這一個」的圖，而那要有人真的去看圖才發現得了。

    兩件事一起釘：
    1. **條目留在佇列裡**——跳過不是完成。吃掉的話，使用者永遠不會知道那一對從來
       沒有產出過，而它已經從待辦裡消失了。
    2. **下一對照樣跑**——游標往前一格，不是整輪停掉。一格填不進去通常是一次性的
       DOM 時序問題，讓它終止整批是過度反應。

    第二對的 char2 刻意留空：`fill_char2` 只在 `entry2` 非空且跟上一輪不同時才跑，
    所以要讓那一步有機會失敗，兩對都得有 Character 2。
    """
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        h.write_queue("todo_character2.md", ["x", "y"])
        _fail_only(h, step)
        aborts = []
        h.patch("_abort_if_chrome_crashed",
                lambda _port, where: aborts.append(where))
        _run(FakeBrowserPort())
        names = [c["name"] for c in h.gen_calls]
        prompts_left = h.read_lines("todo_prompt.md")
        chars_left = h.read_lines("todo_character1.md")
    out = capsys.readouterr().out
    assert message in out, f"沒有說明為什麼跳過：{out[-400:]}"
    assert "a" not in names, "填不進去卻還是產圖了——畫面狀態是錯的"
    assert "a" in prompts_left or "P1" in prompts_left, (
        f"跳過的那一對被吃掉了：prompt={prompts_left} char1={chars_left}")
    assert aborts, "跳過時沒有做崩潰檢查——真的掛了的話會一路空轉到輪尾"
    assert aborts[0].startswith(where + " ("), (
        f"崩潰檢查帶的位置名不是這一步的：{aborts[0]!r}，預期 {where!r}")


@pytest.mark.parametrize("step, message, where", _SKIP_STEPS,
                         ids=[s for s, _m, _w in _SKIP_STEPS])
def test_a_skipped_pair_still_lets_the_next_one_generate(step, message,
                                                         where):
    """游標往前一格，不是整輪停掉。

    跟上面拆成兩支是刻意的：上面那支量「壞的那一對」，這支量「好的那一對」。合在
    一起的話，一個「跳過之後直接 break」的實作只會讓其中一半的斷言紅，而閱讀失敗
    訊息的人會先懷疑錯的那一半。
    """
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        h.write_queue("todo_character2.md", ["x", "y"])
        seen = {"n": 0}
        real = ws.with_retry

        def _fake(label, func, *a, **k):
            # 只讓**第一對**的那一步失敗。
            if label == step and seen["n"] == 0:
                seen["n"] = 1
                return False
            return real(label, func, *a, **k)
        h.patch("with_retry", _fake)
        _run(FakeBrowserPort())
        names = [c["name"] for c in h.gen_calls]
    assert names, f"第一對跳過之後整輪就停了（{step}）"


# ---------------------------------------------------------------------------
# 週期性的 Chrome 重啟（記憶體回收）
# ---------------------------------------------------------------------------

def test_cycling_chrome_forces_every_field_to_be_refilled():
    """重啟之後那三行 `prev_* = None` 是承重的，而漏掉它**不會當場壞掉**。

    每一對只在「值跟上一對不同」時才重填欄位。重啟換來的是一個**空白的新分頁**，
    所以若不把比較用的快取清掉，下一對會以為「主提示詞沒變、不用填」，於是那一對
    以空白的提示詞產圖——畫面上一切正常，圖是錯的。

    語料刻意讓兩對的主提示詞**相同**：不同的話，重填本來就會發生，這條斷言就量不
    到快取有沒有被清掉（等於「斷言恰好等於預設值」那個形狀）。
    """
    with _RunBatchHarness() as h:
        h.cfg_over = {"restart_chrome_every_n_characters": 1}
        h.write_queue("todo_prompt.md", ["SAME", "SAME"])
        h.write_queue("todo_character1.md", ["a", "b"])
        port = _RestartablePort()
        _run(port)
        fills = list(h.main_values)
        restarts = list(port.restarts)
        events = h.events_of("chrome_restart")
    assert restarts, "設定要求每個角色後重啟，卻一次都沒重啟"
    assert len(events) == len(restarts), (
        f"重啟了 {len(restarts)} 次卻發了 {len(events)} 則事件")
    assert fills.count("SAME") >= 2, (
        f"重啟之後沒有重填主提示詞（只填了 {fills}）——下一對會用空白的欄位產圖")


def test_chrome_is_not_cycled_when_nothing_is_left_to_generate():
    """最後一個角色做完之後不重啟——重啟再拆掉是純浪費，而且那是每一輪都會發生的。

    判準是**偷看下一圈會不會 BREAK**（`read_queues()` ＋ `decide()`），不是「佇列
    是不是空的」：padding 與 fallback 都會讓「看起來空了」跟「真的沒事做了」分家。
    """
    with _RunBatchHarness() as h:
        h.cfg_over = {"restart_chrome_every_n_characters": 1}
        _one_pair(h)
        port = _RestartablePort()
        _run(port)
    assert port.restarts == [], (
        "佇列已經抽乾了還重啟一次瀏覽器，下一步就是拆掉它")


def test_chrome_is_never_cycled_when_the_setting_is_zero():
    """反面對照：0 ＝ 關閉。少了它，把 `restart_every_chars > 0` 拿掉也會綠。"""
    with _RunBatchHarness() as h:
        h.cfg_over = {"restart_chrome_every_n_characters": 0}
        h.write_queue("todo_prompt.md", ["P1", "P2"])
        h.write_queue("todo_character1.md", ["a", "b"])
        port = _RestartablePort()
        _run(port)
    assert port.restarts == []


# ---------------------------------------------------------------------------
# resume 檢查點的兩個邊界
# ---------------------------------------------------------------------------

def _checkpoint(folder_name, saved, target, *, prompt="P1", char1="a",
                char2="", undesired=""):
    """寫一份檢查點，寫法跟正式路徑完全一樣。

    ⚠️ `folder` 欄位是 `output/` 底下的**單層資料夾名**，不是路徑——`resume_folder`
    會擋掉任何含 `/` `\\` `:` 的值（理由見那支的 docstring：一個帶路徑的值會讓
    整個角色的圖寫到 `output/` 外面，而且沒有人會發現）。傳整個路徑進來的話這裡
    會安靜地回 None，於是測試量到的是「檢查點不可用」而不是它想量的那一格。

    身分是四個欄位的全等比對（`matches`），所以這四個值必須跟迴圈算出來的那一對
    一字不差，否則接不上。
    """
    import _run_progress as rp
    rp.write_progress(prompt, char1, char2, undesired, folder_name, target)
    if saved:
        rp.update_saved(saved)


def test_a_checkpoint_with_nothing_on_disk_yet_restarts_from_image_one(capsys):
    """上一輪在存下第一張之前就被打斷——**正常情形，不是故障**。

    所以這一格只留 log、不發 `resume_unusable` 事件。發了的話，每一次在第一張之前
    被中斷（重開機、`/gen stop`）都會產生一則「異常」，而真正的異常會淹沒在裡面。
    """
    with _RunBatchHarness() as h:
        _one_pair(h)
        folder = h.dir / "output" / "a_empty"
        folder.mkdir(parents=True)
        _checkpoint("a_empty", saved=0, target=1)
        _run(FakeBrowserPort())
        unusable = h.events_of("resume_unusable")
        resumed = [c["resume_count"] for c in h.gen_calls]
    out = capsys.readouterr().out
    assert "has no images on disk yet" in out, out[-400:]
    assert unusable == [], "正常的中斷被報成異常了"
    assert resumed == [0], f"應該從第 1 張重來：{resumed}"


def test_a_checkpoint_that_is_already_complete_is_reused_and_then_popped(capsys):
    """上一輪做完了、卻在 pop 之前就掛掉：沿用那個資料夾，讓 pop 這次補上。

    重新配一個資料夾的話會得到兩個內容相同的角色目錄，而佇列條目仍然在——也就是
    **同一個角色被做第二次**，而磁碟上多一份沒人要的複本。
    """
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 2}
        _one_pair(h)
        folder = h.dir / "output" / "a_done"
        folder.mkdir(parents=True)
        for i in range(2):
            (folder / f"{i}.png").write_bytes(b"x")
        _checkpoint("a_done", saved=2, target=2)
        seen = []

        def _gen(_port, _name, _cfg, _start, out_dir=None, resume_count=0,
                 **_kw):
            # 真的那一支回的是**這個角色到目前為止的總數**（接續的張數加上這一輪
            # 新產的），所以已經做完時它回 `resume_count` 而不是 0——下面的 pop
            # 門檻比的就是這個值。夾具那個固定回傳的替身在這裡會把 pop 擋掉。
            seen.append(out_dir)
            return resume_count
        h.patch("generate_loop", _gen)
        _run(FakeBrowserPort())
        out_dirs = list(seen)
        left = h.read_lines("todo_character1.md")
    printed = capsys.readouterr().out
    assert "already complete" in printed, printed[-400:]
    assert out_dirs == [folder], f"沒有沿用檢查點的資料夾：{out_dirs}"
    assert left == [], f"沿用之後仍然要 pop，佇列卻還留著：{left}"


# ---------------------------------------------------------------------------
# pop 的兩道守衛、fallback、以及帶內插播
# ---------------------------------------------------------------------------

def test_an_in_band_single_image_forces_the_next_pair_to_refill():
    """角色之間插一張即時單圖，會把主提示詞／角色／負面欄位整組覆寫掉。

    所以服務完必須把比較用的快取清掉，否則下一對的 per-pair diff 會以為「值沒變、
    不用填」，於是那一對用**插播那張圖留下的值**產圖。跟瀏覽器重啟後那三行是同一
    個理由，也是同一種看不出來的錯。

    兩對的主提示詞刻意相同——不同的話重填本來就會發生，這條斷言就量不到快取。
    """
    with _RunBatchHarness() as h:
        h.write_queue("todo_prompt.md", ["SAME", "SAME"])
        h.write_queue("todo_character1.md", ["a", "b"])
        served = []
        h.patch("check_single_image_request",
                lambda _port, in_band=True: (served.append(1)
                                             or len(served) == 2))
        _run(FakeBrowserPort())
        fills = list(h.main_values)
    assert len(served) >= 2, "插播那一格沒有被問到第二次，這一支沒測到東西"
    assert fills.count("SAME") >= 2, (
        f"插播之後沒有重填主提示詞（只填了 {fills}）——那一對會沿用插播留下的值")


def test_a_checkpoint_with_a_nonsense_saved_count_falls_back_to_the_disk():
    """`saved` 欄位不是非負整數時當成 0，讓磁碟上的檔數說了算。

    檢查點是**使用者改得動的本機檔案**，而且會在硬砍之後被半寫。負數或字串直接
    參與 `max(existing, stored_saved)` 的話，接續的起點會被算成負的或炸 `TypeError`
    ——前者讓 `generate_loop` 多產幾張，後者讓整輪批次在起始路徑上死掉。
    """
    with _RunBatchHarness() as h:
        h.cfg_over = {"images_per_character": 3}
        _one_pair(h)
        folder = h.dir / "output" / "a_part"
        folder.mkdir(parents=True)
        (folder / "0.png").write_bytes(b"x")
        _checkpoint("a_part", saved=0, target=3)
        import _run_progress as rp
        prog = rp.read_progress()
        prog["saved"] = "毀損的值"
        rp._atomic_write(prog)
        seen = []
        h.patch("generate_loop",
                lambda *_a, resume_count=0, **_k: (seen.append(resume_count)
                                                   or 3))
        _run(FakeBrowserPort())
    assert seen == [1], f"壞掉的 saved 沒有退回磁碟上的 1 張：{seen}"


def test_a_fallback_queue_is_never_popped():
    """fallback 來源（`prompt.md` 之類）不是佇列，沒有東西可以 pop。

    無條件對帳的話，每一個角色都會誤判成「被外部改動」並寫出一份多餘的空備份——
    而 `.backup/` 是 `/sys undo` 的來源，灌滿空備份等於把還原點沖掉。
    """
    with _RunBatchHarness() as h:
        # 主提示詞走 fallback，角色佇列是真的。
        (h.dir / "prompt.md").write_text("FALLBACK", encoding="utf-8")
        h.write_queue("todo_character1.md", ["a"])
        _run(FakeBrowserPort())
        names = [c["name"] for c in h.gen_calls]
        char1_left = h.read_lines("todo_character1.md")
        # ⚠️ 要在 `with` **裡面**讀：離開時 tmp 目錄整個被刪掉。
        fallback_text = (h.dir / "prompt.md").read_text(encoding="utf-8")
    assert names == ["a"], names
    assert char1_left == [], "真的佇列那一半照樣要 pop"
    assert fallback_text == "FALLBACK", "fallback 來源被當成佇列改掉了"


def test_a_queue_edited_while_generating_is_not_clobbered_by_the_pop():
    """產圖期間使用者把佇列改了——pop 只在游標那一筆**還是剛消耗的那一筆**時才動手。

    一個角色要跑幾十分鐘，中途編輯佇列是常態操作。少了這道 front-match 守衛，pop
    會照位置砍掉**別人剛加進去的那一行**，而使用者只會發現自己加的角色莫名其妙
    不見了——沒有任何錯誤訊息。
    """
    with _RunBatchHarness() as h:
        # ⚠️ 對帳時會把**原磁碟內容**備份到 `<PROJECT_ROOT>/.backup/`，而那個目的地是
        # 在函式裡用 `PROJECT_ROOT` 現組的、沒有自己的常數可以導——不改的話這支會
        # 真的寫進 repo 的 `.backup/`（conftest 的防線會當場攔下並在 teardown 報錯）。
        h.patch("PROJECT_ROOT", h.dir)
        _one_pair(h)
        seen = []

        names = []

        def _gen(_port, character_name, *_a, **_k):
            # 每一圈進來時先記下佇列**現在**長什麼樣，再（只在第一圈）模擬使用者
            # 的編輯。判斷點是第二圈看到的內容：那時第一圈的 pop 已經跑完。
            names.append(character_name)
            seen.append(h.read_lines("todo_character1.md"))
            if len(seen) == 1:
                h.write_queue("todo_character1.md", ["使用者剛改成這個"])
            return 999
        h.patch("generate_loop", _gen)
        _run(FakeBrowserPort())
    assert len(seen) >= 2, f"第二圈沒跑到，判斷點不存在：{seen}"
    assert seen[1] == ["使用者剛改成這個"], (
        f"使用者產圖期間的編輯被第一圈的 pop 蓋掉了：{seen[1]}")
    # 編輯活下來之後，它自己會在下一圈被正常消耗——這才是「不蓋掉」的完整樣子，
    # 不是「從此卡住不動」。
    assert names[-1] == "使用者剛改成這個", names


def test_the_undesired_queue_is_popped_and_says_how_many_remain(capsys):
    """負面提示詞佇列跟其他三個一樣會被消耗，而且要報剩幾筆。

    它是四個佇列裡唯一一個「空字串也是合法內容」的，所以最容易在重構時被當成
    可有可無而漏掉——漏掉的話那一行會一直套用到後面每一個角色身上。
    """
    with _RunBatchHarness() as h:
        _one_pair(h, prompts=("P1", "P2"), chars=("a", "b"))
        h.write_queue("todo_undesired.md", ["U1", "U2"])
        _run(FakeBrowserPort())
        left = h.read_lines("todo_undesired.md")
    out = capsys.readouterr().out
    assert "popped undesired entry" in out, out[-400:]
    assert left == [], f"負面提示詞佇列沒有被消耗完：{left}"


def test_a_fallback_only_run_stops_after_a_single_character(capsys):
    """真實佇列全空、只有 fallback 時**只產一個角色就收工**。

    不收工的話，長度 1 的 fallback 會把抽乾的佇列無限延長成同一個角色的幽靈批次
    ——沒有任何東西會停下來，因為那一行永遠在。
    """
    with _RunBatchHarness() as h:
        (h.dir / "prompt.md").write_text("FALLBACK", encoding="utf-8")
        (h.dir / "character1.md").write_text("CHAR", encoding="utf-8")
        port = _RestartablePort()
        _run(port)
        names = [c["name"] for c in h.gen_calls]
    assert len(names) == 1, f"fallback 跑了不只一個角色：{names}"
    assert port.restarts == [], "收工前不該再重啟一次瀏覽器"


def test_a_fallback_run_does_not_back_up_a_queue_it_never_consumed():
    """fallback 那條路連**對帳**都不做，不只是不 pop。

    為什麼分得出來（變異測試量出來的）：fallback 只在真實佇列**空的時候**才啟用，
    所以正常情況下對帳兩邊都是空的、比起來相等，拿掉那道 `is_fb` 守衛看不出差別
    ——第一版的測試就是這樣讓那個變異活下來的。差別只在**使用者在產圖期間往佇列裡
    加東西**的時候：那時對帳會偵測到「磁碟變了」，於是**備份一份**再採用磁碟版本。
    備份本身無害，但 `.backup/` 是 `/sys undo` 的來源，每個角色灌一份沒人要的備份
    會把真正的還原點擠出去。

    也就是說：這一格要**在對的時機**加東西才量得到。
    """
    with _RunBatchHarness() as h:
        h.patch("PROJECT_ROOT", h.dir)          # `.backup/` 是用它現組的
        (h.dir / "prompt.md").write_text("FALLBACK", encoding="utf-8")
        (h.dir / "character1.md").write_text("CHAR", encoding="utf-8")

        def _gen(*_a, **_k):
            # 產圖期間使用者往**真實**佇列裡加了一行。
            h.write_queue("todo_prompt.md", ["使用者剛加的"])
            return 999
        h.patch("generate_loop", _gen)
        _run(FakeBrowserPort())
        backups = sorted(p.name for p in (h.dir / ".backup").glob("*")
                         ) if (h.dir / ".backup").exists() else []
        added = h.read_lines("todo_prompt.md")
    assert backups == [], (
        f"fallback 那條路對一個它從來沒消耗過的佇列做了對帳並留下備份：{backups}")
    assert added == ["使用者剛加的"], f"使用者剛加的那一行不見了：{added}"
