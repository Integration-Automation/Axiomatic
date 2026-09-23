"""`_chrome_slot` 的行為測試——特別是**搶佔（steal）那條路**。

這個模組守的是整套系統最硬的那條不變量：同一時間只有一個 Chrome stack。它 214 行、
被 bot 的每一次 spawn 與驗證腳本的每一次啟動呼叫，而在這支檔案出現之前**一支行為測試
都沒有**（只有 `test_pid_liveness` 靜態掃它的 `_pid_alive`）。

之所以先寫這一支，是因為 2026-08-30 在這裡實測重現了一個真的競態：

    A 判定持有者 stale → B 也判定同一個持有者 stale
    → B unlink、B create（B 拿到槽，鎖檔是 B 的、活的）
    → A unlink（**把 B 剛建好的活鎖刪掉**）、A create
    → 兩邊都回 True

搶佔不是原子操作，它是「確認 stale → 刪掉舊的 → 建立新的」三步，而原本的實作對第二步
的 `os.unlink` 沒有任何條件——註解寫「若中間被別人搶先，create 會失敗 → 回 False」，
但 create 不會失敗，因為 unlink 已經先把別人的鎖清掉了。

後果正好是本模組存在的理由被推翻：bot 拿到「槽」之後會做 nuclear sweep（無條件殺光所有
`chrome.exe` / `chromedriver.exe`）再 spawn webrunner，而驗證瀏覽器此時正開著、也以為
自己持有槽。這是安靜的——兩邊都收到 True，沒有任何錯誤訊息。

修法是兩層，兩層都在這裡釘住：第二把 `O_CREAT|O_EXCL` 的 marker 把搶佔序列化，以及在
刪舊鎖之前**重讀一次**確認持有者沒換人。marker 自己過期時只能「清掉」不能「順便搶」，
否則同樣的 bug 只是下沉一層。
"""
import inspect
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

# 不可能存在的 pid（Windows 的 pid 是 DWORD 且為 4 的倍數，這個值兩邊都不成立）。
DEAD_PID = 99_999_999


@pytest.fixture
def slot(tmp_path, monkeypatch):
    """把鎖檔與 marker 指到 tmp_path，絕不碰 repo root 上正式的那一把。"""
    import _chrome_slot as cs

    monkeypatch.setattr(cs, "LOCK_PATH", tmp_path / "chrome_slot.lock")
    monkeypatch.setattr(cs, "STEAL_MARKER_PATH",
                        tmp_path / "chrome_slot.steal.lock")
    return cs


def _as_pid(monkeypatch, value: int) -> None:
    """假裝我們是另一個行程。monkeypatch 保證還原（連斷言炸掉也會）。"""
    monkeypatch.setattr(os, "getpid", lambda: value)


def _hold(cs, pid, *, age: float = 0.0, owner: str = "other") -> dict:
    """手工寫一份持有者 metadata，回傳寫進去的內容。"""
    meta = {"pid": pid, "owner": owner, "label": "",
            "acquired_at": time.time() - age}
    cs.LOCK_PATH.write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _holder(cs) -> dict:
    return json.loads(cs.LOCK_PATH.read_text(encoding="utf-8"))


# --- 基本互斥 ---------------------------------------------------------------

def test_an_empty_slot_is_acquired(slot):
    assert slot.try_acquire("bot") is True
    assert _holder(slot)["owner"] == "bot"
    assert _holder(slot)["pid"] == os.getpid()


def test_a_live_recent_holder_is_refused(slot, monkeypatch):
    # 持有者是**這個**行程（保證活著），而我們假裝自己是別人。
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    assert slot.try_acquire("bot") is False
    assert _holder(slot)["owner"] == "verify", "活著的持有者被搶走了"


def test_the_same_process_re_acquiring_is_idempotent(slot):
    assert slot.try_acquire("bot", label="first") is True
    first = _holder(slot)
    assert slot.try_acquire("bot", label="second") is True
    assert _holder(slot) == first, "重複取得不應改寫 metadata"


def test_a_dead_holder_is_stolen(slot):
    _hold(slot, DEAD_PID, owner="ghost")
    assert slot.try_acquire("bot") is True
    assert _holder(slot)["owner"] == "bot"


def test_a_holder_past_its_deadline_is_stolen(slot, monkeypatch):
    # pid 活著，但持有太久 → 時間 backstop 接手。
    _hold(slot, os.getpid(), age=1200.0, owner="verify")
    _as_pid(monkeypatch, 4242)
    assert slot.try_acquire("bot", stale_after=600.0) is True
    assert _holder(slot)["owner"] == "bot"


def test_a_holder_inside_its_deadline_is_left_alone(slot, monkeypatch):
    _hold(slot, os.getpid(), age=100.0, owner="verify")
    _as_pid(monkeypatch, 4242)
    assert slot.try_acquire("bot", stale_after=600.0) is False


# --- 搶佔競態（這支檔案的主角）---------------------------------------------

def test_two_stealers_do_not_both_get_the_slot(slot, monkeypatch):
    """A 與 B 同時判定同一個持有者 stale，只能有一個拿到槽。

    用「在 A 的 `_is_stale` 回 True 之後、動手搶之前，把 B 的整個 `try_acquire` 塞進去」
    來製造確定性的交錯——這正是修好之前會讓 A 把 B 剛建好的活鎖刪掉的那個時間點。
    """
    _hold(slot, DEAD_PID, age=10_000.0, owner="ghost")

    real_is_stale = slot._is_stale
    fired = {"n": 0}

    def is_stale_then_let_b_run(holder, stale_after):
        verdict = real_is_stale(holder, stale_after)
        if verdict and fired["n"] == 0:
            fired["n"] = 1
            _as_pid(monkeypatch, 222)
            assert slot.try_acquire("B") is True, "B 應該搶得到那個死掉的持有者"
            _as_pid(monkeypatch, 111)
        return verdict

    monkeypatch.setattr(slot, "_is_stale", is_stale_then_let_b_run)
    _as_pid(monkeypatch, 111)
    a_won = slot.try_acquire("A")

    assert fired["n"] == 1, "交錯沒有發生，這支測試沒測到東西"
    assert a_won is False, (
        "A 也拿到了槽——搶佔把 B 剛建好的**活鎖**刪掉了。"
        "兩個行程同時以為自己持有單一 Chrome 槽，bot 的 nuclear sweep 會殺掉"
        "驗證瀏覽器。")
    assert _holder(slot)["owner"] == "B"
    assert _holder(slot)["pid"] == 222


def test_a_steal_never_deletes_a_lock_that_changed_hands(slot, monkeypatch):
    """直接對 `_steal` 施壓：拿著一份過期的 holder 快照去搶一把已經換人的鎖。"""
    stale_snapshot = {"pid": DEAD_PID, "owner": "ghost", "label": "",
                      "acquired_at": time.time() - 10_000}
    fresh = _hold(slot, os.getpid(), owner="verify")   # 換人了，而且活著
    _as_pid(monkeypatch, 4242)

    assert slot._steal(stale_snapshot, "bot", "") is False
    assert _holder(slot) == fresh, "把別人新的鎖刪掉了"


def test_the_same_pid_re_acquiring_counts_as_a_new_holder(slot):
    """`_same_holder` 必須連 `acquired_at` 一起比。

    同一個 pid 放掉鎖又重新拿一次，是**新的一次持有**——之前的 stale 判定不再適用。
    只比 pid 的話，一份陳年快照可以把同一個 pid 剛拿到的新鎖搶走。
    """
    old = {"pid": 777, "owner": "verify", "label": "",
           "acquired_at": time.time() - 10_000}
    new = {"pid": 777, "owner": "verify", "label": "",
           "acquired_at": time.time()}
    assert slot._same_holder(old, old) is True
    assert slot._same_holder(old, new) is False


# --- 搶佔 marker ------------------------------------------------------------

def test_a_steal_marker_blocks_a_concurrent_steal(slot, monkeypatch):
    _hold(slot, DEAD_PID, owner="ghost")
    slot.STEAL_MARKER_PATH.write_text("{}", encoding="utf-8")
    _as_pid(monkeypatch, 4242)

    assert slot.try_acquire("bot") is False
    assert _holder(slot)["owner"] == "ghost", "別人正在搶的時候不該動那把鎖"
    assert slot.STEAL_MARKER_PATH.exists(), "還沒過期的 marker 不該被清掉"


def test_a_stale_steal_marker_is_cleared_but_not_used_in_the_same_round(
        slot, monkeypatch):
    """清 marker 的人不可以在同一輪接著搶——否則兩個行程可以同時清、同時搶，
    這把 marker 要防的 bug 就只是下沉一層而已。"""
    _hold(slot, DEAD_PID, owner="ghost")
    slot.STEAL_MARKER_PATH.write_text("{}", encoding="utf-8")
    old = time.time() - (slot.STEAL_MARKER_STALE_SEC + 60)
    os.utime(slot.STEAL_MARKER_PATH, (old, old))
    _as_pid(monkeypatch, 4242)

    assert slot.try_acquire("bot") is False, "清掉過期 marker 的那一輪不該搶"
    assert not slot.STEAL_MARKER_PATH.exists(), "過期的 marker 沒被清掉"

    # 下一輪就正常了——大家重新回到 O_CREAT|O_EXCL，由 kernel 決定誰是唯一的搶佔者。
    assert slot.try_acquire("bot") is True
    assert _holder(slot)["owner"] == "bot"


def test_the_steal_marker_is_cleaned_up_after_a_successful_steal(slot):
    _hold(slot, DEAD_PID, owner="ghost")
    assert slot.try_acquire("bot") is True
    assert not slot.STEAL_MARKER_PATH.exists(), (
        "marker 留著會擋住下一次搶佔，直到 30 秒後才被清")


def test_the_steal_marker_is_cleaned_up_when_the_steal_is_declined(
        slot, monkeypatch):
    stale_snapshot = {"pid": DEAD_PID, "owner": "ghost", "label": "",
                      "acquired_at": time.time() - 10_000}
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)

    assert slot._steal(stale_snapshot, "bot", "") is False
    assert not slot.STEAL_MARKER_PATH.exists()


def test_the_steal_marker_is_cleaned_up_when_the_create_blows_up(
        slot, monkeypatch):
    """`finally` 要真的蓋住例外路徑，不然一次意外會讓槽卡住 30 秒。"""
    _hold(slot, DEAD_PID, owner="ghost")

    def boom(*_args, **_kwargs):
        raise RuntimeError("deliberate")

    monkeypatch.setattr(slot, "_create_exclusive", boom)
    with pytest.raises(RuntimeError):
        slot.try_acquire("bot")
    assert not slot.STEAL_MARKER_PATH.exists()


def test_the_steal_path_does_not_unlink_the_lock_directly(slot):
    """靜態擋回頭路：`try_acquire` 不可以自己 unlink 鎖檔，一律走 `_steal`。"""
    source = inspect.getsource(slot.try_acquire)
    assert "unlink" not in source, (
        "`try_acquire` 又自己刪鎖檔了。搶佔必須走 `_steal`（marker ＋ 重讀確認），"
        "直接 unlink 會刪掉別人剛建好的活鎖——2026-08-30 實測重現過。")


# --- 讀取的健壯性 -----------------------------------------------------------

def test_a_half_written_lock_is_not_read_as_free(slot, monkeypatch):
    """`_create_exclusive` 先建檔再寫內容，中間有一瞬間是空的。"""
    slot.LOCK_PATH.write_text("", encoding="utf-8")
    _as_pid(monkeypatch, 4242)

    holder = slot.read_holder()
    assert holder is not None, "空檔案被當成沒人持有了"
    assert holder["pid"] is None
    assert slot.try_acquire("bot") is False


def test_a_corrupt_lock_still_expires_on_time(slot, monkeypatch):
    slot.LOCK_PATH.write_text("}{ not json", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(slot.LOCK_PATH, (old, old))
    _as_pid(monkeypatch, 4242)

    assert slot.try_acquire("bot") is True, "壞掉的鎖檔會永遠卡住這個槽"
    assert _holder(slot)["owner"] == "bot"


def test_an_undecodable_lock_still_expires_on_time(slot, monkeypatch):
    """上面那支的孿生兄弟：**解不開的位元組跟解不開的 JSON 是同一件事。**

    2026-09-09 之前不是——`read_text` 的 `UnicodeDecodeError` 被接住後回 `None`，
    而 `None` 在這個模組裡的意思是「檔案不存在」。於是 `acquire()` 走「檔案剛剛消失
    了」那條路、去重試 `O_EXCL` 建立，而檔案就在那裡，必然失敗。**槽因此永久卡住**，
    沒有任何時間 backstop 救得回來。

    這個檔案不是假想的：`_create_exclusive` 先建檔再寫內容，寫到一半被砍就會切在
    多位元組字元中間，而 `label` 是可以帶中文的。
    """
    slot.LOCK_PATH.write_bytes(b"\xff\xfe not utf-8")
    old = time.time() - 10_000
    os.utime(slot.LOCK_PATH, (old, old))
    _as_pid(monkeypatch, 4242)

    assert slot.try_acquire("bot") is True, "解不開的鎖檔會永遠卡住這個槽"
    assert _holder(slot)["owner"] == "bot"


def test_a_fresh_undecodable_lock_reads_as_busy(slot, monkeypatch):
    """還沒過期的解不開鎖檔＝有人持有，兩個方向都要說同一件事。

    反面對照組：少了這一支，「一律回 mtime holder 而且一律算 stale」也會讓上面那支
    通過——而那等於把鎖拿掉。
    """
    slot.LOCK_PATH.write_bytes(b"\xff\xfe not utf-8")
    _as_pid(monkeypatch, 4242)

    assert slot.try_acquire("bot") is False, "剛寫下的鎖檔就被搶走了"
    assert slot.held_by_live_other("bot") is True, (
        "`held_by_live_other` 回 False——診斷會說『沒人持有』，"
        "而 `try_acquire` 同時一直失敗，兩句話互相矛盾。")


def test_a_json_scalar_is_not_a_holder(slot, monkeypatch):
    """合法 JSON 但不是 dict（例如 `null`）也要走 mtime 退路，不能讓 `.get` 炸掉。"""
    slot.LOCK_PATH.write_text("null", encoding="utf-8")
    _as_pid(monkeypatch, 4242)
    holder = slot.read_holder()
    assert isinstance(holder, dict)
    assert holder["pid"] is None


def test_a_missing_lock_reads_as_nobody(slot):
    assert slot.read_holder() is None


# --- release ----------------------------------------------------------------

def test_release_only_deletes_our_own_lock(slot, monkeypatch):
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    slot.release("bot")
    assert slot.LOCK_PATH.exists(), "把別人的鎖釋放掉了"


def test_release_deletes_the_lock_we_hold(slot):
    assert slot.try_acquire("bot") is True
    slot.release("bot")
    assert not slot.LOCK_PATH.exists()


def test_release_is_silent_when_there_is_no_lock(slot):
    slot.release("bot")  # 不得 raise


def test_a_nested_release_drops_the_outer_hold(slot, monkeypatch):
    """重入取得**不計次**，所以巢狀的 `release` 會把外層那一次也一起放掉。

    兩件事合起來才有這個結果，各自看都很合理：`try_acquire` 遇到同一個 pid 直接
    `return True`（沒有加深度），而 `release` 只問「持有者 pid 是不是我」就刪鎖檔。
    於是一段「取得 → 做事 → 放掉」的內層程式碼，會在**外層還以為自己握著**的時候
    把整個槽放掉。

    這個方向是 **fail-open**：鎖安靜地消失，沒有例外、沒有 log，下一個來取的人直接
    成功。所以它比死鎖難查得多——死鎖會停住、會被看見，這個只是讓互斥失效，而症狀
    要等到兩套 Chrome 真的撞在一起才出現。

    **釘住現況是刻意的。** 這不是在說這個語意是對的，而是在說它就是**現在**的語意：
    下一個想在 webrunner 的重啟路徑裡加一組 acquire/release 的人，應該在這裡看到
    後果，而不是在一輪無人值守的批次裡發現。真要改成計次，三個呼叫端都得一起改
    （`start_webrunner.py` 的 `_on_spawn`、`discord_bot` 的取槽策略、`verify_browser`
    的 `_run_with_slot`），否則會反過來變成鎖漏著不放，撐到 staleness backstop 才解。
    """
    assert slot.try_acquire("outer", label="batch") is True
    assert slot.try_acquire("inner", label="restart") is True, "重入應該直接放行"
    assert slot.LOCK_PATH.exists()

    slot.release("inner")          # 內層以為自己只放掉「自己那一次」

    assert not slot.LOCK_PATH.exists(), (
        "巢狀 release 沒有刪掉鎖檔——release 被改成計次的了？那三個呼叫端要一起檢查")

    # 外層此刻完全看不出自己已經沒有槽了，而別的行程可以直接取得：這就是 fail-open
    # 的樣子，也是為什麼它不會有任何症狀。
    _as_pid(monkeypatch, 4242)
    assert slot.try_acquire("someone-else") is True, (
        "外層還以為自己握著，別人卻取得成功了——這一行如果紅了，代表語意變了")


# --- held_by_live_other ------------------------------------------------------

def test_held_by_live_other_ignores_our_own_lock(slot):
    assert slot.try_acquire("bot") is True
    assert slot.held_by_live_other() is False


def test_held_by_live_other_ignores_a_stale_holder(slot, monkeypatch):
    _hold(slot, DEAD_PID, owner="ghost")
    _as_pid(monkeypatch, 4242)
    assert slot.held_by_live_other() is False


def test_held_by_live_other_sees_a_live_stranger(slot, monkeypatch):
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    assert slot.held_by_live_other() is True


# --- acquire（阻塞版）--------------------------------------------------------

def test_acquire_without_a_timeout_tries_exactly_once(slot, monkeypatch):
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    assert slot.acquire("bot") is False
    assert slept == [], "timeout<=0 不該睡"


def test_acquire_gives_up_at_the_deadline(slot, monkeypatch):
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    assert slot.acquire("bot", timeout=1.0, poll=0.5) is False
    assert slept, "有 timeout 卻沒有輪詢"
    assert all(s >= 0.05 for s in slept), "poll 太小會變成忙碌等待"


def test_acquire_returns_as_soon_as_the_lock_frees_up(slot, monkeypatch):
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)

    def free_it(_seconds):
        slot.LOCK_PATH.unlink()

    monkeypatch.setattr(time, "sleep", free_it)
    assert slot.acquire("bot", timeout=5.0, poll=0.1) is True


# --- _pid_alive 的邊界 -------------------------------------------------------

@pytest.mark.parametrize("pid", [None, 0, -1])
def test_a_nonsense_pid_is_never_alive(slot, pid):
    assert slot._pid_alive(pid) is False


def test_our_own_pid_is_alive(slot):
    assert slot._pid_alive(os.getpid()) is True


# --- 落地位置 ---------------------------------------------------------------

def test_both_lock_files_live_in_the_repo_root():
    """兩把鎖都必須在 repo root——bot、啟動器、驗證腳本靠的是同一個絕對位置，
    任何一邊改成別的目錄就等於靜靜地解除互斥。"""
    import _chrome_slot as cs

    root = Path(__file__).resolve().parent.parent
    assert cs.LOCK_PATH.parent == root
    assert cs.STEAL_MARKER_PATH.parent == root
    assert cs.STEAL_MARKER_PATH != cs.LOCK_PATH


# ---------------------------------------------------------------------------
# 阻塞式取槽的截止時刻：時鐘被調整也要照樣到期
#
# `acquire()` 等的是一段**間隔**，而 `time.time()` 是可調整的（NTP 的 step 修正、
# 手動改時鐘、虛擬機快照還原——**換時區與日光節約時間不會**，它回的是 UTC epoch 秒）。往回撥一小時會讓一次 45 秒的等待變成一小時——`verify_browser`
# 就這樣掛在那裡不動，而外面看起來只是「驗證好像卡住了」；往前撥則讓 `timeout` 完全
# 失效、退化成只試一次。
#
# 這裡**不** monkeypatch `time.monotonic`：正確的實作根本不看 `time.time()`，所以
# 把牆鐘往回撥就是最直接的證明。測試用「輪詢次數上限」收斂，不是靠真的等——一個會
# 卡住的測試比一個會紅的測試糟得多。
# ---------------------------------------------------------------------------

class _rewind_wall_clock:
    """讓 `time.time()` 每次被呼叫都回到一小時前。"""

    def __init__(self, monkeypatch, seconds: float = 3600.0):
        self._mp = monkeypatch
        self._offset = seconds

    def __enter__(self):
        real = time.time
        self._mp.setattr(time, "time", lambda: real() - self._offset)
        return self

    def __exit__(self, *exc):
        return False


def _capped_sleep(monkeypatch, limit: int):
    """數輪詢次數，超過 `limit` 次就拋——避免測試卡住而不是變紅。

    **仍然呼叫真的 sleep**：完全不睡的話迴圈會用最快的速度空轉到真實時間走完
    `timeout`，次數是幾千次而不是 `timeout / poll` 次，上限就失去意義了。
    """
    real_sleep = time.sleep
    calls: list[float] = []

    def _sleep(seconds):
        calls.append(seconds)
        if len(calls) > limit:
            raise AssertionError(
                f"輪詢超過 {limit} 次還沒到期——截止時刻被時鐘調整拖走了")
        real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", _sleep)
    return calls


def test_a_rewound_clock_does_not_extend_the_wait(slot, monkeypatch):
    """**這一支是缺陷的直接反例。**

    修好之前：`deadline = time.time() + 0.5`，而 `time.time()` 每次都回到一小時前，
    所以 `time.time() >= deadline` 永遠不成立，迴圈一直轉下去。
    """
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    calls = _capped_sleep(monkeypatch, 200)
    with _rewind_wall_clock(monkeypatch):
        assert slot.acquire("bot", timeout=0.5, poll=0.05) is False
    assert calls, "有 timeout 卻完全沒輪詢"


def test_a_fast_forwarded_clock_does_not_cut_the_wait_short(slot, monkeypatch):
    """往前撥不能讓 `timeout` 失效。

    退化成「只試一次」的方向看起來安全（不會多開一套 Chrome），但它讓
    `verify_browser` 在正式作業剛好放手的前一刻就放棄——本來等得到的。
    """
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    calls = _capped_sleep(monkeypatch, 200)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)
    # `stale_after` 開大是為了把**兩條**受牆鐘影響的路分開：時間式 staleness
    # 也吃 `time.time()`（見下一支測試），往前撥一小時會讓現任持有者看起來過期、
    # 於是直接被搶走。這一支要驗的是截止時刻，不是那個。
    assert slot.acquire("bot", timeout=0.5, poll=0.05,
                        stale_after=1e9) is False
    assert calls, "時鐘往前跳讓 timeout 直接失效了——完全沒有輪詢"


def test_a_big_forward_jump_can_still_steal_a_live_holder(slot, monkeypatch):
    """**已知且接受的取捨，不是缺陷——所以在這裡寫下來。**

    `_is_stale` 的時間 backstop 是 `time.time() - acquired_at > stale_after`，而
    `acquired_at` 是**別的行程**寫進鎖檔的。跨行程比較時間點只能用共用的時鐘，
    也就是牆鐘——`monotonic` 的零點每個行程都不一樣，寫進檔案毫無意義。所以這一條
    沒辦法跟著 `acquire` 的截止時刻一起改成單調時鐘。

    後果：時鐘往前跳超過 `stale_after`（預設 600 秒）時，一個**還活著**的持有者會被
    判成過期而遭搶佔 → 兩套 Chrome 同時跑，也就是這個模組存在的理由被推翻。

    為什麼接受：槽被持有的時間很短（bot 的臨界區只有 spawn 那幾秒，
    `verify_browser` 總時限 240 秒），一次超過十分鐘的往前跳必須**正好落在那個
    窗口裡**才會出事。相對地，把時間 backstop 拿掉的代價是確定的——持有者當掉又
    無法判定存活時（沒有 psutil 的機器上 `_pid_alive` 一律回 True），槽永遠不會被
    回收。

    這支測試釘的是「現況如此」，不是「應該如此」。哪天要改，改的方向是讓持有者
    定期更新鎖檔、把判定換成別的東西，而不是把 `stale_after` 調小。
    """
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600.0)
    assert slot.acquire("bot", timeout=0.0, stale_after=600.0) is True, (
        "往前跳一小時竟然沒有讓持有者看起來過期——`_is_stale` 的時間 backstop "
        "可能被拿掉了，那樣持有者當掉又測不出存活時，槽就永遠回收不了")


def test_the_slot_still_frees_up_under_a_rewound_clock(slot, monkeypatch):
    """時鐘怎麼跳都不該影響「鎖放開了就立刻拿到」。"""
    _hold(slot, os.getpid(), owner="verify")
    _as_pid(monkeypatch, 4242)

    def free_it(_seconds):
        slot.LOCK_PATH.unlink()

    monkeypatch.setattr(time, "sleep", free_it)
    with _rewind_wall_clock(monkeypatch):
        assert slot.acquire("bot", timeout=5.0, poll=0.1) is True


def test_the_wait_uses_a_monotonic_clock_and_the_lock_file_uses_the_wall_one(slot):
    """兩種時鐘各司其職，而且分界線寫在原始碼上。

    `acquire` 等的是間隔 → 必須 `monotonic`；鎖檔裡的 `acquired_at`、steal marker
    的 mtime 比對是**跨行程**的時間點 → 必須 `time.time()`（`monotonic` 的零點每個
    行程都不一樣，寫進檔案毫無意義）。把任何一邊「統一」掉都會壞，而且都不會報錯。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(slot.acquire)))
    used = {ast.unparse(n.func) for n in ast.walk(tree)
            if isinstance(n, ast.Call)}
    assert "time.monotonic" in used, "取槽等待又改回牆上時鐘了"
    assert "time.time" not in used, (
        "`acquire` 裡出現了 `time.time()`——它等的是間隔，不是時間點")

    # `acquired_at` 是 `_write_meta` 寫的（`_create_exclusive` 委派給它）。
    write_src = inspect.getsource(slot._write_meta)
    assert "time.time()" in write_src, (
        "鎖檔的 `acquired_at` 改用 monotonic 了——那個值要跨行程比對，"
        "monotonic 的零點每個行程都不一樣，寫進檔案毫無意義")
    assert "time.monotonic" not in write_src


# ---------------------------------------------------------------------------
# `_pid_alive` 的行為測試（2026-09-08 補）
#
# 實測 coverage：這支 25 行只跑過 7 行，**18 行從來沒被執行過**——包含整個
# 「沒有 psutil」的分支。而那個分支裡有一條 CLAUDE.md 的跨領域硬規則：
# **Windows 上絕不能讓 `os.kill(pid, 0)` 執行**（`signal.CTRL_C_EVENT == 0`，
# CPython 會把它導向 `GenerateConsoleCtrlEvent`，對整個 console process group 送出
# 真正的 Ctrl+C）。
#
# 那條規則目前只有 `test_pid_liveness.py` 的**靜態**掃描在守。靜態守門守不住
# 「有沒有真的生效」——本專案已經踩過一次（`if False and not decided:` 讓名字還在、
# AST 照樣過）。所以這裡補行為測試：直接證明那條路上 `os.kill` **一次都沒被呼叫**。
#
# 兩個替換都只動 `_chrome_slot` **模組內部**的名字，不碰真的 `os` / `psutil`：
# `setattr(os, "name", ...)` 會影響整個行程（含其他測試與背景執行緒），
# 而 `sys.modules.pop("psutil")` 更糟——它不是「模擬不存在」，下一次 import 會把
# 真的 psutil 載回來，2026-09-07 就是這樣殺掉一個跑了 78.7 小時的正式批次。
# ---------------------------------------------------------------------------

class _KillWatcher:
    """假的 `os`：記下 `kill` 有沒有被呼叫，並可指定 `name` 與要丟的例外。"""

    def __init__(self, name: str, raises: BaseException | None = None):
        self.name = name
        self.calls: list = []
        self._raises = raises

    def kill(self, pid, sig):
        self.calls.append((pid, sig))
        if self._raises is not None:
            raise self._raises


@pytest.fixture
def cs():
    import _chrome_slot
    return _chrome_slot


@pytest.mark.parametrize("pid", [None, 0, -1, -99])
def test_a_nonsense_pid_is_dead_without_probing(cs, monkeypatch, pid):
    """None／0／負數一律當死的，而且**不得動用任何探測**。"""
    watcher = _KillWatcher("posix")
    monkeypatch.setattr(cs, "os", watcher)
    monkeypatch.setattr(cs, "psutil", None)
    assert cs._pid_alive(pid) is False
    assert watcher.calls == [], "對一個不合法的 pid 還是去探測了"


def test_psutil_answers_are_passed_through(cs, monkeypatch):
    class _P:
        @staticmethod
        def pid_exists(_pid):
            return True

    monkeypatch.setattr(cs, "psutil", _P)
    assert cs._pid_alive(1234) is True

    class _Q:
        @staticmethod
        def pid_exists(_pid):
            return False

    monkeypatch.setattr(cs, "psutil", _Q)
    assert cs._pid_alive(1234) is False


def test_a_psutil_failure_reads_as_alive_not_dead(cs, monkeypatch):
    """psutil 自己炸掉時要保守回 True。

    這個模組的「判斷不出來」**刻意**倒向「活著」（與 `_process_control` 相反，
    見 CLAUDE.md）：這裡問的是「我該不該去搶這個槽」，答錯成 False 會讓第二套
    Chrome stack 起來，而正式產圖程式根本不會來搶這把鎖、擋不住它。
    """
    class _Boom:
        @staticmethod
        def pid_exists(_pid):
            raise RuntimeError("psutil 壞了")

    monkeypatch.setattr(cs, "psutil", _Boom)
    assert cs._pid_alive(1234) is True


def test_on_windows_without_psutil_it_never_calls_os_kill(cs, monkeypatch):
    """**這一支是整組裡最重要的。**

    沒有 psutil 的 Windows 上必須直接回 True，**連 `os.kill` 都不可以碰**——
    signal 0 在 Windows 上不是探測，是對整個 console process group 送出真正的
    Ctrl+C。只斷言回傳值是不夠的：一個「先 kill 再回 True」的實作也會回 True，
    而它每次探測都會對某個行程群組送一次 Ctrl+C。
    """
    watcher = _KillWatcher("nt")
    monkeypatch.setattr(cs, "os", watcher)
    monkeypatch.setattr(cs, "psutil", None)
    assert cs._pid_alive(1234) is True
    assert watcher.calls == [], (
        "Windows 上呼叫了 `os.kill`——signal 0 會送出真正的 Ctrl+C，"
        "這是 CLAUDE.md 的跨領域硬規則")


def test_on_posix_without_psutil_it_probes_with_signal_zero(cs, monkeypatch):
    """POSIX 上才可以用 `os.kill(pid, 0)`，而且訊號必須是 0（不送訊號）。"""
    watcher = _KillWatcher("posix")
    monkeypatch.setattr(cs, "os", watcher)
    monkeypatch.setattr(cs, "psutil", None)
    assert cs._pid_alive(4321) is True
    assert watcher.calls == [(4321, 0)], f"探測方式不對：{watcher.calls}"


@pytest.mark.parametrize("error,expected", [
    (ProcessLookupError(), False),   # 確定不存在
    (PermissionError(), True),       # 存在但無權限
    (OSError(), True),               # 判斷不出來 -> 保守
])
def test_posix_probe_errors_fall_the_documented_way(cs, monkeypatch,
                                                    error, expected):
    """POSIX 探測的三種例外各自倒向哪一邊，是**這個模組刻意與別人不同**的地方。

    `_process_control` 把判斷不出來映成 False、這裡與 `verify_browser` 映成 True。
    CLAUDE.md 明文寫著這個分歧是刻意的、不得「統一」，而 `test_pid_liveness` 只在
    靜態層面守它——這裡從行為上再釘一次。
    """
    watcher = _KillWatcher("posix", raises=error)
    monkeypatch.setattr(cs, "os", watcher)
    monkeypatch.setattr(cs, "psutil", None)
    assert cs._pid_alive(4321) is expected
    assert watcher.calls == [(4321, 0)]
