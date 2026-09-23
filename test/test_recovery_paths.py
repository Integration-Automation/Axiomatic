"""「bot 死過一次之後才會跑的那些程式碼」的守門。

挑這六支函式的判準不是體積也不是重要性，是**它們什麼時候才會第一次執行**：

| 函式 | 第一次執行的時機 |
|---|---|
| `_dorossi_restore_queued_turns_once` / `_dorossi_restore_queue_group` | 重啟後還原排隊中的回合 |
| `_dorossi_resume_loop` | 自走迴圈被中斷後接回去 |
| `_sweep_stale_single_image_state` | 單張產圖的狀態卡住沒收乾淨 |
| `_reap_oneshot_webrunner` | 一次性背景行程沒有正常收屍 |
| `_ensure_background_tasks_alive` | 背景任務死掉了要重新拉起來 |

全部都是「已經出事之後」才跑的復原路徑。這種程式碼「測試綠」跟「它能用」之間沒有
任何關係——它第一次執行的那天，正是最不能出錯的那天。而它們的失敗形態幾乎都不是
丟例外，是**安靜地做錯事**：還原成錯的東西、漏掉該還原的、或什麼都不做。

三個這次抓到的真缺陷，各自留了會紅的測試：

**一、`_sweep_stale_single_image_state` 用牆上時鐘量行程內間隔。**
`_single_image_pending` 是純記憶體的（永不落地），所以「這筆等多久了」是行程內
間隔，只能用單調時鐘。原本用 `time.time()`，兩個方向都會壞而且都是安靜的：往前跳
（開機後 NTP 校正——本機因顯示驅動當機而常重開）會把一筆 5 秒前才送出的 inflight
判成過期，於是拔掉 correlation、刪掉磁碟上的請求檔，等背景行程算完，那個
`request_id` 已經無人認領，圖**根本不會被貼出來**；往後跳則讓整個 TTL 自癒機制
失效，一筆卡住的 inflight 就把整條佇列凍住。同一支函式裡的**檔案 mtime** 比較則
必須留在牆上時鐘（那是別的行程寫的絕對時刻），所以這裡刻意兩個時鐘並存。

**二、收屍收到一半被例外打斷，佇列就再也不會前進。**
`_clear_pid()` 是 `unlink(missing_ok=True)`——`missing_ok` 只吃掉「檔案不存在」，
檔案被鎖住（防毒／索引器正開著）照樣丟 `PermissionError`。而那一行就排在「重新
驅動佇列」前面，所以它一拋，整個復原就停在半路；更糟的是那支 task 沒掛
`_bg_task_done`，連一行帶名字的紀錄都沒有。

**三、`_ensure_background_tasks_alive` 是整條復原鏈的最後一環，但它自己沒有隔離。**
四條背景迴圈原本寫成四個連續的 `if`，第一條 `create_task` 一拋，後面三條就不會被
建立；而 `on_ready` 呼叫它時外面沒有 try，所以連緊接在後的自走迴圈自動接續掃描也
一起沒了，畫面上只留 discord.py 一句 'Ignoring exception in on_ready'。
"""
from __future__ import annotations

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

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
_SRC = (PKG_ROOT / "discord_bot.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _func(name: str):
    """用 AST 取函式節點——不要用 `getsource()` 做子字串比對：docstring 常常就在
    解釋你要檢查的那條規則，於是把程式碼改壞測試照樣綠。"""
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"discord_bot.py 裡找不到 {name}")


# ---------------------------------------------------------------------------
# 共用替身
# ---------------------------------------------------------------------------
class FakeChannel:
    """只長出被測程式碼真的會碰到的那幾個屬性。"""

    guild = None

    def __init__(self, cid: int = 55) -> None:
        self.id = cid
        self.sent: list = []
        # 還原會向平台查回「當初那一則訊息」（`_resolve_trigger_message`），發起人以
        # 它的作者為準。None ＝ 預設的 Dorossi 擁有者。
        self.trigger_author: int | None = None

    async def send(self, content=None, **kwargs):
        del kwargs
        self.sent.append(content)
        return None

    async def fetch_message(self, mid):
        del mid
        uid = self.trigger_author
        return FakeMessage(b.DOROSSI_USER_ID if uid is None else uid, self)


class FakePlaceholder:
    def __init__(self) -> None:
        self.content = None

    async def edit(self, content=None, **kwargs):
        del kwargs
        self.content = content


class FakeAuthor:
    def __init__(self, uid: int) -> None:
        self.id = uid


class FakeMessage:
    def __init__(self, uid: int, channel: FakeChannel | None = None) -> None:
        self.author = FakeAuthor(uid)
        self.channel = channel or FakeChannel()
        self.guild = None
        self.mentions = []
        self.content = ""
        self.id = 4242
        self.replies: list = []

    async def reply(self, content=None, **kwargs):
        del kwargs
        self.replies.append(content)
        return None


@pytest.fixture(autouse=True)
def _isolate_recovery_state(monkeypatch, tmp_path):
    """把這批函式會**寫到磁碟**與**改到模組全域**的東西全部導開。

    這台機器上有一個跑了好幾天的正式批次，所以絕對不能讓測試碰到真正的
    `webrunner.pid`、產圖請求檔或 Dorossi 佇列檔。"""
    monkeypatch.setattr(b, "AUDIT_FILE", tmp_path / "audit.ndjson")
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", tmp_path / "webrunner.pid")
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", tmp_path / "q.ndjson")
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", tmp_path / "qf.ndjson")
    # 服務看門狗取消一筆時會寫產圖歷史、也會看暫停標記——兩個都是 repo root 的
    # 活檔案，不導開就會在這台正在跑正式批次的機器上動到真的東西。
    monkeypatch.setattr(b, "GENERATE_HISTORY_FILE", tmp_path / "gh.ndjson")
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", tmp_path / "webrunner.pause")
    # 產圖佇列 / correlation map 是模組層可變物件，逐一還原比 monkeypatch 可靠。
    saved = (list(b._generate_queue), dict(b._single_image_pending),
             b._generate_inflight, b._dorossi_queue_restored,
             dict(b._dorossi_session_locks), dict(b._dorossi_session_lock_refs),
             dict(b._dorossi_loops), b._webrunner_proc, b._webrunner_pid,
             b._webrunner_variant, b._webrunner_oneshot,
             b._webrunner_oneshot_reaper_task)
    b._generate_queue.clear()
    b._single_image_pending.clear()
    b._generate_inflight = None
    b._dorossi_session_locks.clear()
    b._dorossi_session_lock_refs.clear()
    b._dorossi_loops.clear()
    saved_live = set(b._dorossi_queue_live)
    b._dorossi_queue_live.clear()
    yield
    b._dorossi_queue_live.clear()
    b._dorossi_queue_live.update(saved_live)
    (queue, pending, inflight, restored, locks, refs, loops,
     proc, pid, variant, oneshot, reaper) = saved
    b._generate_queue[:] = queue
    b._single_image_pending.clear()
    b._single_image_pending.update(pending)
    b._generate_inflight = inflight
    b._dorossi_queue_restored = restored
    b._dorossi_session_locks.clear()
    b._dorossi_session_locks.update(locks)
    b._dorossi_session_lock_refs.clear()
    b._dorossi_session_lock_refs.update(refs)
    b._dorossi_loops.clear()
    b._dorossi_loops.update(loops)
    b._webrunner_proc = proc
    b._webrunner_pid = pid
    b._webrunner_variant = variant
    b._webrunner_oneshot = oneshot
    b._webrunner_oneshot_reaper_task = reaper


def _shift_wall_clock(monkeypatch, seconds: float) -> None:
    """只動牆上時鐘，**不動** `time.monotonic`。

    兩個一起假造會讓 `asyncio` 的計時（它就是用 monotonic）跟著錯亂，測試會因為
    一個跟被測行為無關的理由變紅或掛住。這裡要量的正是「這兩個時鐘被混用了沒」，
    所以只准動一個。"""
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + seconds)


def _submit(request_id: str, *, age_sec: float = 0.0,
            placeholder=None, queued: bool = False) -> FakePlaceholder:
    """造一筆「`age_sec` 秒前送出」的單張產圖請求（用單調時鐘記時間）。"""
    ph = placeholder or FakePlaceholder()
    ctx = {"channel_id": 2, "message_id": 3, "user_id": 1,
           "submitted_mono": time.monotonic() - age_sec}
    b._single_image_pending[request_id] = ctx
    if queued:
        b._generate_queue.append(b._GenerateRequest(request_id, {}, ctx, ph))
    return ph


# ===========================================================================
# 一、`_sweep_stale_single_image_state` —— 兩個時鐘，兩個方向
# ===========================================================================

def test_a_forward_clock_jump_cannot_expire_a_fresh_request(monkeypatch):
    """開機後 NTP 把時鐘往前校正，不得把剛送出的請求判成過期。

    這是原本的真缺陷：TTL 用 `time.time()` 量一個純記憶體的間隔，時鐘往前跳一小時
    就等於「所有請求瞬間老了一小時」。後果最嚴重的是 inflight 那一筆——correlation
    被拔掉、磁碟上的請求檔被刪掉，等背景行程算完，`single_image_done` 帶回來的
    `request_id` 已經無人認領，那張圖**安靜地不會被貼出來**，算力也白花了。"""
    async def _body():
        _submit("R1", age_sec=5)
        b._generate_inflight = "R1"
        b.SINGLE_IMAGE_REQUEST_FILE.write_text('{"request_id":"R1"}',
                                               encoding="utf-8")
        ph = _submit("R2", age_sec=3, queued=True)
        _shift_wall_clock(monkeypatch, 3600)
        b._sweep_stale_single_image_state()
        await asyncio.sleep(0)
        return ph

    ph = asyncio.run(_body())
    assert sorted(b._single_image_pending) == ["R1", "R2"], (
        "牆上時鐘往前跳一小時就把 5 秒前送出的請求掃掉了——"
        "行程內間隔要用 `time.monotonic()`，不是 `time.time()`。")
    assert b._generate_inflight == "R1", "inflight 閘門被誤清"
    assert b.SINGLE_IMAGE_REQUEST_FILE.exists(), (
        "磁碟上的請求檔被刪掉了——背景行程正要服務它，刪掉就等於那張圖永遠不會回來。")
    assert [r.request_id for r in b._generate_queue] == ["R2"]
    assert ph.content is None, f"排隊者被謊報逾時：{ph.content!r}"


def test_a_backward_clock_jump_cannot_disable_the_ttl_self_heal(monkeypatch):
    """反方向：時鐘往後跳不得讓 TTL 自癒失效。

    往後跳時 `now - submitted_at` 變成負數，於是**永遠不會**超過 TTL——一筆真的
    卡住的 inflight 就把整條佇列凍到時鐘追回來為止，而 TTL 自癒正是為了這種情況
    才存在的。這一支跟上一支必須成對：只修其中一個方向的實作照樣會有一支綠的。"""
    async def _body():
        # 2026-09-12：inflight 已改由服務看門狗判斷（心跳沉默），所以這裡要把
        # 「心跳早就停了」布置出來；牆上時鐘往後跳照樣不得讓自癒失效。
        _submit("OLD", age_sec=b._SINGLE_IMAGE_PENDING_TTL_SEC + 60)
        b._generate_inflight = "OLD"
        now = time.monotonic()
        ctx = b._single_image_pending["OLD"]
        ctx["serve_seen_mono"] = now - 10_000
        ctx["serve_within"] = 285.0
        monkeypatch.setattr(b, "_event_poll_caught_up_mono", now)
        monkeypatch.setattr(b, "_webrunner_liveness", lambda: (True, True))
        b.SINGLE_IMAGE_REQUEST_FILE.write_text("{}", encoding="utf-8")
        pumped: list = []
        monkeypatch.setattr(b, "_generate_pump",
                            lambda: _record_coro(pumped, "pump"))
        _shift_wall_clock(monkeypatch, -3600)
        b._sweep_stale_single_image_state()
        for _ in range(5):
            await asyncio.sleep(0)
        return pumped

    pumped = asyncio.run(_body())
    assert "OLD" not in b._single_image_pending, (
        "牆上時鐘往後跳就讓 TTL 永遠不觸發——卡住的請求會把整條佇列凍住。")
    assert b._generate_inflight is None, "卡住的 inflight 沒有被清掉，佇列不會前進"
    assert not b.SINGLE_IMAGE_REQUEST_FILE.exists()
    assert pumped == ["pump"], "清掉 inflight 之後要重新推一次佇列"


def _record_coro(sink: list, tag: str):
    async def _run():
        sink.append(tag)
    return _run()


def test_the_stranded_request_file_is_swept_on_the_wall_clock(monkeypatch):
    """檔案 mtime 那一段**必須**留在牆上時鐘——它是別的行程寫的絕對時刻。

    `st_mtime` 是 epoch 秒，拿 `time.monotonic()`（開機以來的秒數，零點每個行程
    都不一樣）去減它會得到一個巨大的負數，於是這段清理永遠不觸發，一個沒人服務
    的請求檔會從此擋住所有使用者。所以這支跟上面兩支的方向相反，是刻意的。"""
    async def _body():
        b.SINGLE_IMAGE_REQUEST_FILE.write_text("{}", encoding="utf-8")
        old = time.time() - (b._SINGLE_IMAGE_PENDING_TTL_SEC + 120)
        os.utime(b.SINGLE_IMAGE_REQUEST_FILE, (old, old))
        b._sweep_stale_single_image_state()
        await asyncio.sleep(0)

    asyncio.run(_body())
    assert not b.SINGLE_IMAGE_REQUEST_FILE.exists(), (
        "擱淺的請求檔沒被清掉。mtime 是別的行程寫的絕對時刻，只能跟 "
        "`time.time()` 比；換成單調時鐘會讓這段永遠不觸發。")


def test_a_fresh_request_file_is_never_swept_by_mtime():
    """剛寫好的請求檔不准被 mtime 那一段誤刪（背景行程正要接手它）。"""
    async def _body():
        b.SINGLE_IMAGE_REQUEST_FILE.write_text("{}", encoding="utf-8")
        b._sweep_stale_single_image_state()
        await asyncio.sleep(0)

    asyncio.run(_body())
    assert b.SINGLE_IMAGE_REQUEST_FILE.exists()


def test_a_queued_request_is_not_expired_by_its_submit_age():
    """（2026-09-12 取代「過期的排隊項要被丟掉」）佇列裡的請求只等閘門前進，而閘門
    現在由服務看門狗保證會前進。用送出年齡收它們，等於在一張健康的長服務後面把排隊
    的人一個個取消——而這支掃描現在也會被**定時**跑，那就成了確定性的誤殺。
    """
    async def _body():
        ph = _submit("Q", age_sec=b._SINGLE_IMAGE_PENDING_TTL_SEC + 10,
                     queued=True)
        b._sweep_stale_single_image_state()
        for _ in range(5):
            await asyncio.sleep(0)
        return ph

    ph = asyncio.run(_body())
    assert [r.request_id for r in b._generate_queue] == ["Q"]
    assert "Q" in b._single_image_pending
    assert ph.content is None


def test_an_orphaned_correlation_entry_still_expires():
    """反面對照：既不是 inflight、也不在佇列裡的條目仍然用 TTL 收掉。少了這一支，
    「整段 TTL 掃描刪掉」也會全綠。"""
    _submit("ORPHAN", age_sec=b._SINGLE_IMAGE_PENDING_TTL_SEC + 10)
    _submit("FRESH", age_sec=5)
    b._sweep_stale_single_image_state()
    assert sorted(b._single_image_pending) == ["FRESH"]


def test_the_sweep_measures_in_process_age_on_the_monotonic_clock():
    """AST：TTL 那個減法的左運算元，必須綁在 `time.monotonic()` 上。

    行為測試已經蓋到兩個時鐘方向，但那要靠「湊得出一個會露餡的時鐘偏移」；這一支
    直接對機制設限，改回 `time.time()` 立刻紅。用 AST 而不是字串比對，因為
    docstring 裡就寫著 `time.monotonic`，子字串版本會被自己的說明文件騙過去。"""
    fn = _func("_sweep_stale_single_image_state")
    monotonic_names = {
        target.id
        for node in ast.walk(fn) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
        if isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "time.monotonic"
    }
    subtractions = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
        and "submitted_mono" in ast.unparse(node.right)
    ]
    assert subtractions, (
        "找不到用 `submitted_mono` 算年齡的減法——欄位被改名或那段被拿掉了？")
    for node in subtractions:
        assert isinstance(node.left, ast.Name) and node.left.id in monotonic_names, (
            f"`{ast.unparse(node)}` 的左邊不是綁在 `time.monotonic()` 的變數。"
            "`_single_image_pending` 純記憶體、永不落地，所以那是行程內間隔，"
            "用牆上時鐘會被 NTP 校正兩個方向都打壞。")


# ===========================================================================
# 二、`_reap_oneshot_webrunner` —— 收屍不准停在半路
# ===========================================================================
class FakeProc:
    """`proc.wait()` 會被丟進 executor，所以它必須是同步且會回來的。"""

    def __init__(self, rc: int = 0) -> None:
        self._rc = rc

    def wait(self):
        return self._rc

    def poll(self):
        return self._rc


def _reaper_env(monkeypatch, *, inflight: str | None = "R1"):
    calls: list = []
    # ⚠️ `_generate_retry_server` **必須**在這裡換掉。2026-09-11 起收屍後的再驅動走
    # 它而不是直接 `_generate_ensure_server`；沒換掉的話真的那一支會跑起來、用正式
    # 的退避（5 秒起跳）去睡，而這一族測試只排空幾個 tick，於是什麼都沒記到、斷言
    # 讀起來像「再驅動壞了」。這一步在原本的設計裡被漏掉了，是跑測試才發現的。
    monkeypatch.setattr(b, "_generate_ensure_server",
                        lambda: _record_coro(calls, "ensure"))
    monkeypatch.setattr(b, "_generate_pump",
                        lambda: _record_coro(calls, "pump"))
    monkeypatch.setattr(b, "_generate_retry_server",
                        lambda _rid: _record_coro(calls, "retry"))
    b._generate_inflight = inflight
    b._webrunner_pid = 4242
    b._webrunner_variant = "je"
    b._webrunner_oneshot = True
    b._webrunner_log_handle = None
    return calls


def test_the_reaper_resets_tracking_state_and_redrives_the_queue(monkeypatch):
    """基本盤：行程結束 → tracking state 歸零（`_webrunner_alive()` 才不會說謊）
    → 佇列重新驅動。磁碟上還有 inflight 時只要「確保有 server」，**不要**重寫
    那筆請求（背景行程只在服務完才刪檔，新 server 啟動時會接手它）。"""
    async def _body():
        calls = _reaper_env(monkeypatch)
        proc = FakeProc()
        b._webrunner_proc = proc
        await b._reap_oneshot_webrunner(proc)
        for _ in range(5):
            await asyncio.sleep(0)
        return calls

    calls = asyncio.run(_body())
    assert b._webrunner_proc is None and b._webrunner_pid is None
    assert b._webrunner_oneshot is False
    # 2026-09-11 起走的是重起閥門（`_generate_retry_server`），不再直接
    # `_generate_ensure_server`——直接重起在「起來就死」時是無界迴圈。
    assert calls == ["retry"], f"重新驅動走錯路：{calls}"


def test_the_reaper_pumps_when_nothing_is_on_disk(monkeypatch):
    """磁碟上沒有 inflight 但佇列有人在等 → 要走 pump（它會寫前端那筆再起 server）。"""
    async def _body():
        calls = _reaper_env(monkeypatch, inflight=None)
        b._generate_queue.append(
            b._GenerateRequest("W", {}, {"submitted_mono": time.monotonic()},
                               None))
        proc = FakeProc()
        b._webrunner_proc = proc
        await b._reap_oneshot_webrunner(proc)
        for _ in range(5):
            await asyncio.sleep(0)
        return calls

    assert asyncio.run(_body()) == ["pump"]


def test_the_reaper_still_redrives_when_the_pid_file_cannot_be_deleted(
        monkeypatch):
    """真缺陷：`_clear_pid()` 拋例外就把後面的「重新驅動佇列」整段跳過。

    `unlink(missing_ok=True)` 的 `missing_ok` 只吃掉「檔案不存在」；Windows 上檔案
    被鎖住（防毒／索引器正開著）照樣丟 `PermissionError`。而收屍的最後一步才是
    重新驅動佇列，所以那一拋等於：產圖佇列從此不再前進，要等到有人再送一次請求、
    再等 600 秒的 TTL 掃描才自癒。更糟的是這支 task 沒掛 `_bg_task_done`，
    連一行帶名字的紀錄都不會有。"""
    async def _body():
        calls = _reaper_env(monkeypatch)

        def _locked():
            raise PermissionError(13, "in use")

        monkeypatch.setattr(b, "_clear_pid", _locked)
        proc = FakeProc()
        b._webrunner_proc = proc
        await b._reap_oneshot_webrunner(proc)
        for _ in range(5):
            await asyncio.sleep(0)
        return calls

    calls = asyncio.run(_body())
    assert calls == ["retry"], (
        "清 PID 檔失敗就把重新驅動整段跳過了——產圖佇列會安靜地停在那裡。")
    assert b._webrunner_proc is None, "state 只重設到一半"


def test_the_reaper_never_escapes_with_an_unnamed_exception(monkeypatch,
                                                            capsys):
    """收屍途中任何意外都要留下一行帶名字的紀錄，而且不得往外拋。

    這支 task 是 `_spawn_oneshot_webrunner` 用裸的 `create_task` 建的（要留 handle
    給停止指令取消），**沒有**掛 `_bg_task_done`——例外逸出時 asyncio 只會在 GC 時
    補一句泛用的 'Task exception was never retrieved'，看不出是哪個功能死了。"""
    class Exploding:
        closed = False

        def close(self):
            raise RuntimeError("boom")

        def __bool__(self):
            raise RuntimeError("handle exploded")

    async def _body():
        _reaper_env(monkeypatch)
        b._webrunner_log_handle = Exploding()
        proc = FakeProc()
        b._webrunner_proc = proc
        try:
            await b._reap_oneshot_webrunner(proc)
        finally:
            b._webrunner_log_handle = None

    asyncio.run(_body())     # 不得往外拋
    err = capsys.readouterr().err
    assert "one-shot reaper crashed" in err, (
        f"收屍安靜地死了，stderr 沒有留下可辨識的一行：{err!r}")


def test_the_reaper_leaves_another_runs_state_alone(monkeypatch):
    """double-reset 防護：停止指令／新的批次已經接手時，收屍只觀察、不得覆寫。"""
    async def _body():
        calls = _reaper_env(monkeypatch)
        dead = FakeProc()
        newer = FakeProc()
        b._webrunner_proc = newer          # 別人已經換掉了
        await b._reap_oneshot_webrunner(dead)
        for _ in range(5):
            await asyncio.sleep(0)
        return calls

    calls = asyncio.run(_body())
    assert b._webrunner_proc is not None, "收屍把別人的行程狀態清掉了"
    assert b._webrunner_oneshot is True
    assert calls == [], "不是自己的行程還去重新驅動佇列"


def test_the_reaper_clears_only_its_own_handle():
    """`finally` 只在自己仍是「目前的收屍者」時清 handle，免得清掉下一個。"""
    async def _body():
        proc = FakeProc()
        b._webrunner_proc = None
        other = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        b._webrunner_oneshot_reaper_task = other
        await b._reap_oneshot_webrunner(proc)
        await other
        return b._webrunner_oneshot_reaper_task is other

    assert asyncio.run(_body()), "把別人的收屍 task handle 清掉了"


def test_the_reaper_does_not_probe_liveness_with_signal_zero():
    """收屍路徑不准用 `os.kill(pid, 0)` 探活（Windows 上兩個方向都會答錯）。"""
    fn = _func("_reap_oneshot_webrunner")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            assert ast.unparse(node.func) != "os.kill", (
                "`signal.CTRL_C_EVENT == 0`，所以 signal 0 在 Windows 上會被當成"
                "console process group id 處理，剛結束的子行程會回報「還活著」。")


# ===========================================================================
# 三、`_ensure_background_tasks_alive` —— 復原鏈的最後一環
# ===========================================================================
_LOOP_ATTRS = ("_schedule_task", "_event_watcher_task",
               "_daily_health_task", "_presence_probe_task")


def _stub_loops(monkeypatch) -> list:
    started: list = []

    def _factory(name):
        async def _loop():
            started.append(name)
            await asyncio.Event().wait()
        return _loop

    monkeypatch.setattr(b, "_schedule_loop", _factory("schedule"))
    monkeypatch.setattr(b, "_event_watcher", _factory("event"))
    monkeypatch.setattr(b, "_daily_health_loop", _factory("daily"))
    monkeypatch.setattr(b, "_presence_probe_loop", _factory("presence"))
    for attr in _LOOP_ATTRS:
        monkeypatch.setattr(b, attr, None)
    return started


async def _cancel_loops():
    for attr in _LOOP_ATTRS:
        task = getattr(b, attr)
        if task is not None:
            task.cancel()
    await asyncio.sleep(0)


def test_all_four_loops_come_up_named_and_supervised(monkeypatch):
    """四條迴圈都要有名字、也都要掛上「安靜死掉會留一行」的回呼。

    名字只在它炸掉時看得到，而這四條的失敗形態就是安靜——沒有名字就只知道「有東西
    死了」，不知道是排程、事件監看、健檢還是 presence。"""
    async def _body():
        started = _stub_loops(monkeypatch)
        monkeypatch.setattr(b.client, "loop", asyncio.get_running_loop())
        b._ensure_background_tasks_alive(source="probe")
        await asyncio.sleep(0)
        tasks = [getattr(b, attr) for attr in _LOOP_ATTRS]
        names = [t.get_name() for t in tasks]
        supervised = [bool(t._callbacks) for t in tasks]
        await _cancel_loops()
        return started, names, supervised

    started, names, supervised = asyncio.run(_body())
    assert sorted(started) == ["daily", "event", "presence", "schedule"]
    assert names == ["schedule-loop", "event-watcher", "daily-health",
                     "presence-probe"]
    assert all(supervised), "有迴圈沒掛上 `_bg_task_done`，它死掉時不會有名字"


def test_a_healthy_loop_is_not_restarted_twice(monkeypatch):
    """冪等：連叫兩次不得起第二份（`on_ready` 與 `on_resumed` 都會叫它）。"""
    async def _body():
        started = _stub_loops(monkeypatch)
        monkeypatch.setattr(b.client, "loop", asyncio.get_running_loop())
        b._ensure_background_tasks_alive(source="first")
        await asyncio.sleep(0)
        first = [getattr(b, attr) for attr in _LOOP_ATTRS]
        started.clear()
        b._ensure_background_tasks_alive(source="second")
        await asyncio.sleep(0)
        again = [getattr(b, attr) for attr in _LOOP_ATTRS]
        await _cancel_loops()
        return started, first, again

    started, first, again = asyncio.run(_body())
    assert started == [], f"健康的迴圈被重開了：{started}"
    assert first == again, "task 物件被換掉了——舊的還在跑，等於兩份"


def test_a_crashed_loop_counts_as_dead_and_is_revived(monkeypatch):
    """「已經 raise 但沒人 await」的 task 算死的，必須被重建。

    這正是這支函式存在的理由：`done()` 對「跑完」「拋例外」「被取消」一律為 True，
    所以判活只能是 `task is not None and not task.done()`。改成看 `cancelled()`
    或自己記旗標，一條炸掉的迴圈就會被當成還活著，從此安靜地停止運作。"""
    async def _body():
        started = _stub_loops(monkeypatch)
        monkeypatch.setattr(b.client, "loop", asyncio.get_running_loop())

        async def _boom():
            raise RuntimeError("presence died")

        dead = asyncio.get_running_loop().create_task(_boom())
        dead.add_done_callback(b._bg_task_done)   # 例外被取走，跟正式路徑一樣
        await asyncio.sleep(0)
        monkeypatch.setattr(b, "_presence_probe_task", dead)
        b._ensure_background_tasks_alive(source="revive")
        await asyncio.sleep(0)
        revived = b._presence_probe_task is not dead
        await _cancel_loops()
        return started, revived

    started, revived = asyncio.run(_body())
    assert revived, "炸掉的迴圈被當成還活著——它會安靜地停止運作，沒有任何症狀。"
    assert "presence" in started


def test_one_loop_that_cannot_start_does_not_take_the_others_with_it(
        monkeypatch, capsys):
    """真缺陷：四條迴圈原本是四個連續的 `if`，第一條建不起來就沒有後三條。

    而 `on_ready` 呼叫它時外面沒有 try，所以例外會逸出整個 `on_ready`，連緊接在後
    的自走迴圈自動接續掃描也一起沒了，只留 discord.py 一句
    'Ignoring exception in on_ready'。這是整條復原鏈的最後一環，它自己必須是
    fault-isolated 的。"""
    async def _body():
        started = _stub_loops(monkeypatch)
        monkeypatch.setattr(b.client, "loop", asyncio.get_running_loop())

        def _cannot_build():
            raise RuntimeError("cannot build schedule coro")

        monkeypatch.setattr(b, "_schedule_loop", _cannot_build)
        b._ensure_background_tasks_alive(source="partial")
        await asyncio.sleep(0)
        alive = [attr for attr in _LOOP_ATTRS if getattr(b, attr) is not None]
        await _cancel_loops()
        return started, alive

    started, alive = asyncio.run(_body())
    assert sorted(started) == ["daily", "event", "presence"], (
        f"一條迴圈建不起來就拖垮了其他三條：{started}")
    assert "_schedule_task" not in alive
    assert "could not be started" in capsys.readouterr().err, (
        "失敗的那一條要留下一行帶名字的紀錄，否則沒人知道它沒起來")


def test_the_liveness_check_never_treats_a_finished_task_as_alive():
    """AST：判活條件必須包含 `not <task>.done()`。

    行為測試示範的是「炸掉的 task 會被重建」，但一個把條件改成
    `task is None` 的版本在**第一次**呼叫時行為完全一樣（那時本來就是 None），
    要靠上面那支特定情境才會露餡。這支直接對條件本身設限。"""
    fn = _func("_ensure_background_tasks_alive")
    done_calls = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "done"
    ]
    assert done_calls, (
        "判活條件裡沒有 `.done()`——那會把一條已經拋例外的迴圈當成還活著，"
        "而它的失敗形態是完全沒有症狀，只是東西悄悄停止運作。")


# ===========================================================================
# 四、Dorossi 佇列還原 —— 還原成對的東西，而且只還原一次
# ===========================================================================
def _row(qid: str, **over) -> dict:
    row = {"id": qid, "uid": str(b.DOROSSI_USER_ID), "sid": "s1", "channel_id": 55,
           "message_id": 9, "prompt": f"prompt-{qid}",
           "created_at": time.time()}
    row.update(over)
    return row


def _restore_env(monkeypatch, turn=None):
    channel = FakeChannel()
    monkeypatch.setattr(b.client, "get_channel", lambda cid: channel)
    seen: list = []

    async def _default_turn(message, prompt, placeholder, uid, sid, **kwargs):
        del message, placeholder
        seen.append((uid, sid, prompt, kwargs))

    monkeypatch.setattr(b, "_dorossi_process_turn", turn or _default_turn)
    return channel, seen


def test_restoring_runs_each_persisted_turn_exactly_once(monkeypatch):
    """基本盤：每筆持久化的回合各跑一次，微調（effort／model）要跟著還原。"""
    async def _body():
        _channel, seen = _restore_env(monkeypatch)
        b._dorossi_queue_write([
            _row("q1", effort="high", model_tier="opus"),
            _row("q2", sid="s2"),
        ])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return seen

    seen = asyncio.run(_body())
    assert sorted(x[2] for x in seen) == ["prompt-q1", "prompt-q2"]
    by_prompt = {x[2]: x for x in seen}
    assert by_prompt["prompt-q1"][3] == {"effort": "high", "model_tier": "opus",
                                         "usage_parks": 0}
    assert by_prompt["prompt-q2"][3] == {"effort": None, "model_tier": None,
                                         "usage_parks": 0}
    assert b._dorossi_queue_read() == [], "跑完了還留在磁碟上——下次重啟會再跑一次"


def test_a_row_cancelled_while_waiting_its_turn_is_not_run(monkeypatch):
    """同一個 session 的幾列是依序重跑的，後面那幾列要等前一列跑完才拿得到鎖。等的期間擁有者
    下 `/dorossi abort` 或 `/dorossi queue clear`，那一列會從磁碟上拿掉、bot 回「不會再跑」——
    但重跑那一組手上還握著它。原本照跑不誤（2026-09-23 補上）：開始前要回頭看它還在不在。"""
    async def _body():
        seen: list = []

        async def _turn(message, prompt, placeholder, uid, sid, **kwargs):
            del message, placeholder, uid, sid, kwargs
            seen.append(prompt)
            if prompt == "prompt-q1":
                b._dorossi_queue_remove("q2")      # 跑第一列的時候，第二列被取消了

        _restore_env(monkeypatch, turn=_turn)
        rows = [_row("q1"), _row("q2"), _row("q3")]
        b._dorossi_queue_write(rows)
        await b._dorossi_restore_queue_group(rows[0]["uid"], "s1", rows)
        return seen

    seen = asyncio.run(_body())
    assert seen == ["prompt-q1", "prompt-q3"], seen
    assert b._dorossi_queue_read() == []


@pytest.mark.parametrize("action", ["undo", "retry_failed"])
def test_requeueing_runs_a_cancelled_row_again_and_only_once(monkeypatch, action):
    """`/dorossi queue undo`／`retry_failed` 把列放回去重跑：要真的跑，而且只跑一次。

    取消（或停進失敗佇列）會把那一列從佇列檔上拿掉；重跑那一組開始前又會回頭確認它還在不在
    （上一支）。所以兩個入口都必須先把列放回磁碟，否則等於沒復原。另外模擬「原本那一組還握著
    同一列」：兩組同時排上去，在同一把 session 鎖上排隊，只准跑一次。"""
    async def _body():
        seen: list = []

        async def _turn(message, prompt, placeholder, uid, sid, **kwargs):
            del message, placeholder, uid, sid, kwargs
            seen.append(prompt)
            await asyncio.sleep(0)

        _restore_env(monkeypatch, turn=_turn)
        row = _row("q7")
        b._dorossi_queue_write([])                  # 已經被拿掉了
        if action == "undo":
            b._dorossi_queue_undo.clear()
            b._dorossi_queue_undo.append([dict(row)])
        else:
            b._dorossi_failed_queue_write([dict(row, failed_at=1.0, error="x")])
        scheduled: list = []
        monkeypatch.setattr(b, "_schedule_coro",
                            lambda coro, label="": scheduled.append(coro))
        monkeypatch.setattr(b, "_dorossi_owner_only", lambda _m: True)

        async def _reply(*_a, **_k):
            return None

        monkeypatch.setattr(b, "safe_reply", _reply)
        await b.mcmd_queue(types.SimpleNamespace(
            author=types.SimpleNamespace(id=b.DOROSSI_USER_ID)), action)
        assert [r["id"] for r in b._dorossi_queue_read()] == ["q7"]
        assert "q7" in b._dorossi_queue_live
        assert len(scheduled) == 1, scheduled
        original = b._dorossi_restore_queue_group(row["uid"], "s1", [dict(row)])
        await asyncio.gather(original, *scheduled)
        return seen

    seen = asyncio.run(_body())
    assert seen == ["prompt-q7"], seen
    assert b._dorossi_queue_read() == []


def test_a_second_on_ready_in_the_same_process_restores_nothing(monkeypatch):
    """`once` 的重點在於 `on_ready` **不只在行程啟動時跑**。

    gateway session 過期到無法 RESUME 時 discord.py 會重新 IDENTIFY，同一個行程會
    再觸發一次 `on_ready`。少了旗標，那一次重連就會把**當下活著的排隊項**（它們的
    列還在磁碟上、也還有各自的等待者）當成「上一個行程留下來的」再還原一次——
    同一個提問跑兩遍，使用者收到兩份答案，帳單也算兩次。"""
    async def _body():
        _channel, seen = _restore_env(monkeypatch)
        b._dorossi_queue_write([_row("live")])
        b._dorossi_queue_restored = True    # 這個行程已經還原過了
        b._dorossi_restore_queued_turns_once()
        for _ in range(20):
            await asyncio.sleep(0)
        return seen

    assert asyncio.run(_body()) == [], (
        "重新 IDENTIFY 觸發的第二次 on_ready 又還原了一次——同一個回合會跑兩遍。")
    assert [r["id"] for r in b._dorossi_queue_read()] == ["live"], (
        "而且它還把活著的排隊項從磁碟上刪掉了")


def test_a_row_stays_on_disk_as_running_until_its_turn_finishes(monkeypatch):
    """跑的時候那一列還在，標成 `running`、次數已經先加一；跑完才拿掉（2026-09-22）。

    以前是刻意的 at-most-once（開始就刪），代價是回合進行中 bot 被砍、或網路斷了，
    那一題就消失了。擋「會把行程弄死的提問無限重啟」的責任改由次數承擔（下一支）：
    次數**先落地再跑**，所以死在半路的那一次也算數。"""
    async def _body():
        during: list = []

        async def _turn(message, prompt, placeholder, uid, sid, **kwargs):
            del message, prompt, placeholder, uid, sid, kwargs
            during.extend((r["id"], r.get("status"), r.get("tries"))
                          for r in b._dorossi_queue_read())

        _restore_env(monkeypatch, turn=_turn)
        b._dorossi_queue_write([_row("q1")])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return during

    assert asyncio.run(_body()) == [("q1", "running", 1)]
    assert b._dorossi_queue_read() == [], "跑完了還留在磁碟上——下次重啟會再跑一次"


def test_a_row_that_already_died_mid_turn_twice_is_parked_not_rerun(monkeypatch):
    """上一個行程跑到一半死掉的列會再跑一次；死過兩次就停進失敗佇列——那正是
    at-most-once 原本要擋的「會把行程弄死的提問，每次重啟都再弄死一次」。"""
    async def _body():
        _channel, seen = _restore_env(monkeypatch)
        b._dorossi_queue_write([_row("dead", status="running", tries=2),
                                _row("once", status="running", tries=1, sid="s2")])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return seen

    seen = asyncio.run(_body())
    assert [x[2] for x in seen] == ["prompt-once"], "死過兩次的那一列不該再跑"
    failed = b._dorossi_failed_queue_read()
    assert [r["id"] for r in failed] == ["dead"]
    assert failed[0]["error"] == "interrupted"


def test_a_failed_restore_is_parked_not_lost(monkeypatch):
    """還原失敗的回合要進失敗佇列（擁有者能 `retry_failed`），不能安靜蒸發。"""
    async def _body():
        async def _boom(message, prompt, placeholder, uid, sid, **kwargs):
            del message, prompt, placeholder, uid, sid, kwargs
            raise ValueError("backend exploded")

        channel, _seen = _restore_env(monkeypatch, turn=_boom)
        b._dorossi_queue_write([_row("q1")])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return channel

    channel = asyncio.run(_body())
    failed = b._dorossi_failed_queue_read()
    assert [r["id"] for r in failed] == ["q1"], "失敗的回合不見了"
    assert failed[0]["error"] == "ValueError"
    assert failed[0]["failed_at"] > 0
    assert channel.sent, "失敗了卻沒告訴任何人"


def test_a_failed_restore_tells_a_non_owner_nothing_raw(monkeypatch):
    """Secrecy Layer 1：非擁有者只能拿到泛用句，原始例外文字不得外送。

    2026-09-19 起還原會先向平台查回發起人，而且那個發起人要過 Dorossi 自己的閘——
    所以「非擁有者的排隊提問被還原、跑到一半炸掉」這個狀態**已經走不到了**：那一列
    在跑之前就被停進失敗佇列（`test_a_restored_row_runs_only_as_the_verified_owner`
    專門測那一道）。這裡留著的是那個狀態的後半：就算有一列冒名、而且在頻道裡講了話，
    送出去的也只有泛用句，被測的回合一次都沒跑、原始例外一個字都沒出去。擁有者那一半
    照舊拿得到原始細節。"""
    ran: list = []

    async def _body(uid: str):
        async def _boom(message, prompt, placeholder, u, sid, **kwargs):
            del message, prompt, placeholder, sid, kwargs
            ran.append(u)
            raise RuntimeError(r"D:\Work\Example\todo_prompt.md 壞了")

        channel, _seen = _restore_env(monkeypatch, turn=_boom)
        channel.trigger_author = int(uid)
        b._dorossi_queue_write([_row("q1", uid=uid)])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return channel.sent

    stranger = asyncio.run(_body("7"))
    assert stranger, "測試前提壞了：沒送出任何訊息"
    assert ran == [], "非擁有者的排隊提問被還原執行了"
    blob = "\n".join(str(x) for x in stranger)
    assert "todo_prompt.md" not in blob and "D:\\" not in blob, (
        f"原始例外文字外送給了非擁有者：{blob!r}")
    assert "RuntimeError" not in blob

    owner = asyncio.run(_body(str(b.DOROSSI_USER_ID)))
    assert ran == [str(b.DOROSSI_USER_ID)]
    assert "todo_prompt.md" in "\n".join(str(x) for x in owner), (
        "擁有者裁定「if asker is owner then no limit」——擁有者要拿得到原始細節。")


# ---------------------------------------------------------------------------
# 四之二、還原的發起人以平台的紀錄為準，不信佇列檔裡的 uid（2026-09-19）
#
# 佇列檔是本機檔案、手改得動。原本 `_DorossiRestoredAuthor` 把列上的 uid 直接變成
# `author.id`——一筆改過的列就能讓還原出來的回合（以及它可能轉進的 full 工具模式
# 自走迴圈）以任何人的身分過閘。跨重啟自動接續同一天修掉了同一類問題；還原現在也用
# `_resolve_trigger_message` 查回發起人。每一種拒絕只踩一道條件。
# ---------------------------------------------------------------------------
_BOT = 999_000_111
_SLASH_ID = 600000000000000003


def _meta_reply(mid=_SLASH_ID, invoker=None):
    """bot 回覆那一次互動的訊息（斜線起的提問，平台上留下的紀錄就是它）。"""
    msg = FakeMessage(_BOT)
    msg.interaction_metadata = types.SimpleNamespace(
        id=mid, type=b.discord.InteractionType.application_command,
        user=FakeAuthor(b.DOROSSI_USER_ID if invoker is None else invoker))
    return msg


class _SlashChannel(FakeChannel):
    """斜線起的提問：直接抓 interaction id 是 404，要從歷史裡找 bot 的回覆。"""

    def __init__(self, history=(), history_error=None) -> None:
        super().__init__()
        self._history, self._error = list(history), history_error
        self.references: list = []

    async def send(self, content=None, **kwargs):
        self.references.append(kwargs.get("reference"))
        return await super().send(content, **kwargs)

    async def fetch_message(self, mid):
        raise b.discord.NotFound(types.SimpleNamespace(status=404, reason="nf"),
                                 {"code": 10008, "message": "Unknown Message"})

    def history(self, **_kw):
        items, boom = self._history, self._error

        async def _gen():
            for item in items:
                yield item
            if boom is not None:
                raise boom
        return _gen()


def _run_restore(monkeypatch, channel, rows):
    """用真的還原流程跑 `rows`；回 (跑過的回合, 失敗佇列, 事件, 頻道訊息)。"""
    ran: list = []
    events: list = []

    async def _turn(message, prompt, placeholder, uid, sid, **kwargs):
        del placeholder, kwargs
        ran.append((uid, sid, prompt, message.author.id, message.id))
        await b.safe_reply(message, "answer")

    monkeypatch.setattr(b, "_dorossi_process_turn", _turn)
    monkeypatch.setattr(b.client, "get_channel", lambda cid: channel)
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=_BOT), get_channel=lambda cid: channel,
        fetch_channel=b.client.fetch_channel))
    monkeypatch.setattr(b, "_dorossi_event",
                        lambda kind, **data: events.append((kind, data)))

    async def _body():
        b._dorossi_queue_write(rows)
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(60):
            await asyncio.sleep(0)

    asyncio.run(_body())
    return ran, b._dorossi_failed_queue_read(), events, channel.sent


def test_a_slash_origin_row_is_restored_as_the_platform_says(monkeypatch):
    """斜線起的提問：直接抓是 404，從 bot 那則回覆的 `interaction_metadata` 找回發起人。

    跑起來的回合，`message.author` 是平台給的那個人（不是從列上的 uid 組出來的），
    `message.id` 沿用存下來的 interaction id，回覆掛在 bot 那則回覆底下。"""
    reply = _meta_reply()
    channel = _SlashChannel(history=[FakeMessage(7), reply])
    ran, failed, _events, _sent = _run_restore(
        monkeypatch, channel, [_row("q1", message_id=_SLASH_ID)])
    owner = b.DOROSSI_USER_ID
    assert ran == [(str(owner), "s1", "prompt-q1", owner, _SLASH_ID)], ran
    assert failed == []
    assert channel.sent == ["answer"], channel.sent
    assert channel.references == [reply], "回覆沒有掛在 bot 那則回覆底下"


def test_an_at_bot_origin_row_still_restores(monkeypatch):
    """`@bot` 起的提問：抓得到那則訊息，發起人就是它的作者。"""
    channel = FakeChannel()
    ran, failed, _events, _sent = _run_restore(monkeypatch, channel, [_row("q1")])
    assert [r[3] for r in ran] == [b.DOROSSI_USER_ID] and failed == []


@pytest.mark.parametrize("label,make_channel,uid,reason", [
    ("查不回那則訊息", lambda: _SlashChannel(history=[FakeMessage(7)]), None, "anchor"),
    ("翻歷史途中出錯", lambda: _SlashChannel(history=[],
                                        history_error=RuntimeError("503")),
     None, "anchor"),
    ("發起人不是這一列的 uid",
     lambda: _SlashChannel(history=[_meta_reply(invoker=4242)]), None,
     "invoker_mismatch"),
    ("發起人與 uid 一致但不是擁有者",
     lambda: _SlashChannel(history=[_meta_reply(invoker=4242)]), "4242",
     "not_owner"),
])
def test_a_restored_row_runs_only_as_the_verified_owner(monkeypatch, label,
                                                       make_channel, uid, reason):
    """每一格只踩一道條件；不成立的列**不跑**、停進失敗佇列、記下原因、並且出聲。

    第四格是專門拆開的：少了它，「必須過 Dorossi 的閘」那一道整段刪掉照樣全綠
    （第三格在 uid 那一道就被擋掉了）。"""
    channel = make_channel()
    row = _row("q1", message_id=_SLASH_ID, uid=uid or str(b.DOROSSI_USER_ID))
    ran, failed, events, sent = _run_restore(monkeypatch, channel, [row])
    assert ran == [], label
    assert [r["id"] for r in failed] == ["q1"], label
    assert failed[0]["error"] == f"unverified:{reason}", (label, failed[0])
    assert ("restore_drop", {"uid": row["uid"], "sid": "s1", "reason": reason}) \
        in events, (label, events)
    assert b._dorossi_queue_read() == [], label
    assert any("沒辦法確認發問者" in str(x) for x in sent), (label, sent)
    assert all("prompt-q1" not in str(x) for x in sent), "提問內容被貼出去了"


def test_a_row_whose_channel_is_gone_is_parked_not_lost(monkeypatch):
    """頻道抓不到也是「查不回來」：停進失敗佇列（原因 channel），不是直接丟掉。"""
    async def _no_fetch(cid):
        raise RuntimeError("gone")

    channel = FakeChannel()
    monkeypatch.setattr(b.client, "fetch_channel", _no_fetch)
    ran: list = []

    async def _turn(*a, **k):
        ran.append(a)

    monkeypatch.setattr(b, "_dorossi_process_turn", _turn)
    monkeypatch.setattr(b.client, "get_channel",
                        lambda cid: channel if cid == b.CHANNEL_ID else None)

    async def _body():
        b._dorossi_queue_write([_row("q1", channel_id=77)])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)

    asyncio.run(_body())
    failed = b._dorossi_failed_queue_read()
    assert ran == [] and [r["error"] for r in failed] == ["unverified:channel"]
    assert channel.sent and "沒辦法確認發問者" in channel.sent[0], channel.sent


def test_retrying_a_parked_row_goes_through_the_same_check(monkeypatch):
    """`/dorossi queue retry_failed` 重試的也要再驗一次——停進失敗佇列不是後門。

    直接叫三個入口共用的那一支，餵一筆冒名的列：不跑、再停一次。"""
    channel = _SlashChannel(history=[_meta_reply(invoker=4242)])
    ran: list = []

    async def _turn(*a, **k):
        ran.append(a)

    monkeypatch.setattr(b, "_dorossi_process_turn", _turn)
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=_BOT), get_channel=lambda cid: channel))
    row = _row("q9", message_id=_SLASH_ID)
    asyncio.run(b._dorossi_restore_queue_group(row["uid"], "s1", [row]))
    assert ran == []
    assert [r["error"] for r in b._dorossi_failed_queue_read()] == [
        "unverified:invoker_mismatch"]


def test_rows_that_can_never_run_are_dropped_not_retried_forever(monkeypatch):
    """跑不動的列（空提問、沒有 uid／sid、頻道已消失）要當場丟掉。

    留著它們只會每次重啟都重試一次，而且會把真正的失敗淹沒在雜訊裡。"""
    async def _body():
        _channel, seen = _restore_env(monkeypatch)
        monkeypatch.setattr(b.client, "get_channel", lambda cid: None)

        async def _no_fetch(cid):
            raise RuntimeError("channel gone")

        monkeypatch.setattr(b.client, "fetch_channel", _no_fetch)
        b._dorossi_queue_write([
            _row("blank", prompt="   "),
            _row("nouid", uid=""),
            _row("gone"),
        ])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(40):
            await asyncio.sleep(0)
        return seen

    assert asyncio.run(_body()) == []
    assert b._dorossi_queue_read() == [], (
        "跑不動的列還留在磁碟上——每次重啟都會再試一次")


def test_the_session_lock_is_balanced_even_when_a_restore_blows_up(monkeypatch):
    """還原用的是正式的 per-session 鎖；漏放一次 ref，那個 session 從此
    「忙線中」，而且只有重啟才會好。所以放鎖必須在 `finally`。"""
    async def _body():
        async def _boom(message, prompt, placeholder, uid, sid, **kwargs):
            del message, prompt, placeholder, uid, sid, kwargs
            raise ValueError("nope")

        _restore_env(monkeypatch, turn=_boom)
        b._dorossi_queue_write([_row("q1"), _row("q2")])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(60):
            await asyncio.sleep(0)

    asyncio.run(_body())
    assert not b._dorossi_session_lock_refs, (
        f"鎖的 ref 沒放乾淨：{b._dorossi_session_lock_refs}——"
        "那個 session 會永遠回「忙線中」。")
    assert not b._dorossi_session_locks


def test_rows_of_one_session_are_replayed_in_order(monkeypatch):
    """同一個 session 的多筆回合要照原順序、且一次只跑一筆（共用一把鎖）。"""
    async def _body():
        order: list = []
        overlap: list = []
        running = {"n": 0}

        async def _turn(message, prompt, placeholder, uid, sid, **kwargs):
            del message, placeholder, uid, sid, kwargs
            running["n"] += 1
            overlap.append(running["n"])
            await asyncio.sleep(0)
            order.append(prompt)
            running["n"] -= 1

        _restore_env(monkeypatch, turn=_turn)
        b._dorossi_queue_write([_row("q1"), _row("q2"), _row("q3")])
        b._dorossi_queue_restored = False
        b._dorossi_restore_queued_turns_once()
        for _ in range(80):
            await asyncio.sleep(0)
        return order, overlap

    order, overlap = asyncio.run(_body())
    assert order == ["prompt-q1", "prompt-q2", "prompt-q3"]
    assert max(overlap) == 1, "同一個 session 的回合並行跑了——resume 會互相汙染"


def test_the_once_flag_is_raised_before_anything_can_fail():
    """AST：`_dorossi_queue_restored = True` 必須是函式裡的第一件事（守衛之後）。

    它排在讀檔之前是刻意的：讀檔或分組若拋例外，這一輪就不還原了，而不是留著讓
    每次重新 IDENTIFY 都再撞一次同一個壞檔。"""
    fn = _func("_dorossi_restore_queued_turns_once")
    body = [node for node in fn.body if not isinstance(node, ast.Expr)]
    assigns = [i for i, node in enumerate(body)
               if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name)
                       and t.id == "_dorossi_queue_restored"
                       for t in node.targets)]
    assert assigns, "找不到把 `_dorossi_queue_restored` 設起來的那一行"
    calls_before = [
        node for node in body[:assigns[0]]
        for sub in ast.walk(node) if isinstance(sub, ast.Call)
    ]
    assert not calls_before, (
        "旗標在做過別的事之後才立起來——中途拋例外就會讓下一次重新 IDENTIFY "
        "再還原一次。")


# ===========================================================================
# 五、`_dorossi_resume_loop` —— 接回被中斷的自走任務
# ===========================================================================
def _resume_env(monkeypatch, *, sess: dict, run=None):
    state = {str(b.OWNER_USER_ID): {"active": "s1", "next_seq": 2,
                                    "sessions": {"s1": sess}}}
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: state)
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "full")
    monkeypatch.setattr(b, "DOROSSI_BACKEND", "claude_code")
    calls: list = []

    async def _default_run(message, task, placeholder, uid, sid, **kwargs):
        del message, placeholder
        calls.append((uid, sid, task, kwargs))

    monkeypatch.setattr(b, "_dorossi_run_loop", run or _default_run)
    return calls


def test_resume_continues_the_same_backend_context_when_it_survived(monkeypatch):
    """後端脈絡還在 → 以 CONTINUE 模式接續同一個工作階段（`already_ran_first`）。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc",
        })
        await b._dorossi_resume_loop(FakeMessage(b.OWNER_USER_ID), None)
        return calls

    calls = asyncio.run(_body())
    assert len(calls) == 1
    assert calls[0][1] == "s1"
    assert calls[0][3]["already_ran_first"] is True, (
        "脈絡還在卻從頭跑——第一輪的成果會被白白重做一次")


def test_resume_restarts_from_the_stored_task_when_context_was_cleared(
        monkeypatch):
    """脈絡被清掉但留有任務描述 → 用原任務文字重新起跑。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True}})
        await b._dorossi_resume_loop(FakeMessage(b.OWNER_USER_ID), None)
        return calls

    calls = asyncio.run(_body())
    assert calls[0][2] == "整理測試"
    assert calls[0][3]["already_ran_first"] is False


def test_resume_refuses_a_non_owner_generically(monkeypatch):
    """閘門與自走一致（擁有者＋工具模式 full）。拒絕一律泛用，而且不得起迴圈。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc"})
        state = b._dorossi_load_state()
        state["999"] = state[str(b.OWNER_USER_ID)]
        message = FakeMessage(999)
        await b._dorossi_resume_loop(message, None)
        return calls, message.replies

    calls, replies = asyncio.run(_body())
    assert calls == [], "非擁有者也能接續一個 full 工具模式的自走迴圈"
    blob = " ".join(str(x) for x in replies)
    assert "claude" not in blob.lower() and "full" not in blob.lower(), (
        f"拒絕訊息洩漏了後端佈線：{blob!r}")


def test_resume_says_so_when_there_is_nothing_to_continue(monkeypatch):
    """沒有 `loop_pending` 標記 → 明確講「沒有可接續的任務」，不要默默起一個空迴圈。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={"cc_session_id": "abc"})
        message = FakeMessage(b.OWNER_USER_ID)
        await b._dorossi_resume_loop(message, None)
        return calls, message.replies

    calls, replies = asyncio.run(_body())
    assert calls == []
    assert any("沒有可接續" in str(x) for x in replies), replies


def test_resume_does_not_start_a_second_loop_on_the_same_session(monkeypatch):
    """同一個 session 已經有迴圈在跑時不得再起一個——兩個行程同時
    `--resume` 同一個工作階段會把它的儲存區弄壞。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc"})
        key = b._dorossi_session_key(str(b.OWNER_USER_ID), "s1")
        b._dorossi_loops[key] = object()
        message = FakeMessage(b.OWNER_USER_ID)
        await b._dorossi_resume_loop(message, None)
        return calls, message.replies

    calls, replies = asyncio.run(_body())
    assert calls == [], "同一個 session 起了第二個自走迴圈"
    assert any("進行中" in str(x) for x in replies), replies


def test_resume_of_a_busy_session_does_not_leak_a_lock_reference(monkeypatch):
    """忙線中的早退路徑必須把剛拿到的那個 ref 放掉。

    漏掉的話那個 session 的鎖與等待佇列永遠不會被回收——每按一次
    「接續」就多洩一個，而症狀只是「這個對話後來怪怪的」。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc"})
        key = b._dorossi_session_key(str(b.OWNER_USER_ID), "s1")
        holder = b._dorossi_acquire_session_lock(key)
        await holder.acquire()
        message = FakeMessage(b.OWNER_USER_ID)
        await b._dorossi_resume_loop(message, None)
        refs = b._dorossi_session_lock_refs.get(key, 0)
        holder.release()
        b._dorossi_release_session_lock(key)
        return calls, message.replies, refs

    calls, replies, refs = asyncio.run(_body())
    assert calls == []
    assert any("忙線" in str(x) for x in replies), replies
    assert refs == 1, (
        f"忙線早退之後 ref 是 {refs}，應該只剩持有者那一個——早退路徑漏放了鎖。")


def test_resume_releases_the_lock_when_the_loop_blows_up(monkeypatch):
    """迴圈拋例外時鎖照樣要放掉（那是它的 `finally`），否則整個 session 卡死。"""
    async def _body():
        async def _boom(message, task, placeholder, uid, sid, **kwargs):
            del message, task, placeholder, uid, sid, kwargs
            raise RuntimeError("loop exploded")

        _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc"}, run=_boom)
        with pytest.raises(RuntimeError):
            await b._dorossi_resume_loop(FakeMessage(b.OWNER_USER_ID), None)

    asyncio.run(_body())
    assert not b._dorossi_session_lock_refs, (
        f"迴圈炸掉之後鎖沒放：{b._dorossi_session_lock_refs}")


def test_resume_of_an_unknown_session_id_is_refused(monkeypatch):
    """指名一個不存在的 session → 拒絕，不得掉回 active session 去接錯任務。"""
    async def _body():
        calls = _resume_env(monkeypatch, sess={
            "loop_pending": {"task": "整理測試", "live": True},
            "cc_session_id": "abc"})
        message = FakeMessage(b.OWNER_USER_ID)
        await b._dorossi_resume_loop(message, "s99")
        return calls, message.replies

    calls, replies = asyncio.run(_body())
    assert calls == [], "指名不存在的 session 卻接了 active session 的任務"
    assert any("找不到" in str(x) for x in replies), replies


# ===========================================================================
# 六、one-shot 重起閥門 —— 起來就死不准變成無界迴圈
# ===========================================================================
class _Placeholder:
    """佔位訊息的替身；只要一個 async 的 `edit(content=...)`。"""

    def __init__(self) -> None:
        self.content: str | None = None

    async def edit(self, *, content: str) -> None:
        self.content = content


def _valve_env(monkeypatch, tmp_path, *, inflight="R1", giveup=3, cap=60):
    """讓**真的** `_generate_ensure_server` / `_spawn_oneshot_webrunner` 跑起來，
    但把每一個會碰到主機的呼叫換掉。回傳「spawn 過的 variant」清單。

    ⚠️ **`_terminate_all_webrunner_instances` 一定要換掉。** 它是 nuclear sweep，
    真的會把開發機上每一個 chrome.exe / chromedriver.exe 殺光，而這條路**每一圈**
    都呼叫它一次。`_browser_killguard`（conftest 載入）擋得住真正的 kill，但靠後備
    網是錯的層級——既有的 `_reaper_env` 沒有換掉它，所以這一族不能用它。

    ⚠️ **`SINGLE_IMAGE_REQUEST_FILE` 一定要改指到 `tmp_path`。**
    `_generate_give_up_inflight` 會 unlink 它，而 repo root 那個檔案是**活的跨行程
    狀態**。

    ⚠️ **假的 spawn 一定要有自己的硬上限。** 這個缺陷的回歸形態是**無界**迴圈，
    沒有上限的話測試會**掛住**而不是變紅，而掛住的測試比紅的測試糟。

    ⚠️ **`_webrunner_liveness` 也要換掉。** `_webrunner_alive()` 走它，而它會去讀
    **磁碟上的** `webrunner.pid`——正式作業在跑的機器上它會老實回 True，於是
    `_generate_ensure_server` 直接 return，測試量到 0 圈、看起來像「沒有缺陷」。
    （這不是假設：第一版探針就是這樣得到一個很有說服力的錯誤答案。）

    ⚠️ **這個替身的簽章必須跟著 `_spawn_webrunner` 走，而且它「漏接」的方式很惡劣。**
    2026-09-12：真的那支加上 keyword-only 的 `single_image_server` 之後，替身還停在
    `def _spawn(variant)`，於是每一次重試都丟 `TypeError`，被收屍那邊的廣域 except
    吞成「一次也沒 spawn」。`giveup=2` / `3` 兩格如預期變紅——但 **`giveup=1` 那格
    照樣是綠的**，因為它期望的就是 0 次，而壞掉的路徑剛好也生出 0 次。替身少一個參數
    ＝ 這一族測試全部退化成「什麼都沒跑」，而其中一格會替它掩護。所以下面錄的是
    **(variant, 旗標)** 而不只是 variant：多錄那一格，就能在計數之外再問一句「重試起
    來的到底是不是單張伺服器」——重試若退化成批次，它會去跑整條 todo 佇列。
    """
    spawns: list[tuple[str, bool]] = []

    def _spawn(variant, *, single_image_server=False):
        spawns.append((variant, single_image_server))
        if len(spawns) > cap:
            b._webrunner_proc = None
            return False, "測試上限"
        b._webrunner_proc = FakeProc(2)
        b._webrunner_pid = 999_999
        b._webrunner_variant = "je"
        return True, "fake"

    async def _acquire(*_a, **_k):
        return True

    async def _terminate(*_a, **_k):
        return None

    monkeypatch.setattr(b, "_spawn_webrunner", _spawn)
    monkeypatch.setattr(b, "_acquire_chrome_slot", _acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda *a, **k: None)
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", _terminate)
    monkeypatch.setattr(b, "_clear_pid", lambda *a, **k: None)
    monkeypatch.setattr(b, "_active_pid", lambda *a, **k: None)
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (
        b._webrunner_proc is not None and b._webrunner_proc.poll() is None, True))
    # ⚠️ **`find_launcher_pids` 也要換掉，而且它漏掉的方式跟這台機器的狀態有關。**
    # 2026-09-12 起 `_generate_ensure_server` 在 spawn 之前會對獨立監督者讓位，而
    # 那道判斷走的是**真的 psutil 掃描**。開發機上常常真的有一個 `start_webrunner.py`
    # 在跑（實測當天就有），於是這一族會全部退化成「讓位、0 次 spawn」——而同一份
    # 程式碼在沒有監督者的機器上是綠的。讓位那條路自己的測試在
    # `test_launcher_command.py`，這裡要驗的是閥門，所以固定成「沒有監督者」。
    monkeypatch.setattr(b, "find_launcher_pids", lambda: ([], True))
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE",
                        tmp_path / "single_image_request.json")
    # 門檻與退避都用**測試自己的**值：讀正式預設的話，把預設調大就會安靜地把測試
    # 弄弱而它仍然是綠的。
    monkeypatch.setattr(b, "GENERATE_ONESHOT_GIVEUP_COUNT", giveup)
    monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MIN_SEC", 0.001)
    monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MAX_SEC", 0.002)
    monkeypatch.setattr(b, "_generate_oneshot_fail_rid", None)
    monkeypatch.setattr(b, "_generate_oneshot_fails", 0)
    monkeypatch.setattr(b, "_generate_oneshot_backoff", 0.001)
    monkeypatch.setattr(b, "_generate_inflight", inflight)
    # ⚠️ 這兩個是模組層**可變物件**，要就地清空、**不要**用 `monkeypatch.setattr`
    # 換掉。autouse 的 `_isolate_recovery_state` 是在 setup 時記下內容、teardown 時
    # 用 `b._generate_queue[:] = ...` 填回去；而 monkeypatch 比它**後**收尾，於是
    # 「填回去」會填進這裡換上的臨時 list，再被 monkeypatch 換回那個已經被清空的
    # 原物件——正式的佇列內容就這樣安靜地掉了。
    b._generate_queue.clear()
    b._single_image_pending.clear()
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)
    monkeypatch.setattr(b, "_webrunner_oneshot", True)
    monkeypatch.setattr(b, "_webrunner_log_handle", None)
    return spawns


async def _drain_until(predicate, *, limit: float = 5.0) -> bool:
    """等到 `predicate()` 成立，最多 `limit` 秒；成立回 True，逾時回 False。

    ⚠️ **不能用 `for _ in range(N): await asyncio.sleep(0)` 排空。** 閥門裡有真的
    `await asyncio.sleep(退避)`，而 `sleep(0)` 只是讓出控制權、**不會讓時鐘前進**，
    所以幾百個 tick 加起來還不到一次退避的長度——測試會在半路收工，量到偏少的圈數
    然後以一個誤導的理由變紅（第一版就是這樣：log 明明印到 `retry 2/3`，spawn 卻
    只數到 1）。
    ⚠️ **也必須有硬上限。** 這個缺陷的回歸形態是**無界**迴圈，沒有上限的話測試會
    掛住，而掛住的測試比紅的測試糟。
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.002)
    return predicate()


@pytest.mark.parametrize("giveup", [1, 2, 3])
def test_a_one_shot_that_never_serves_stops_respawning(monkeypatch, tmp_path,
                                                       giveup):
    """真缺陷：背景程式起來就死時，收屍 → 再驅動 → 再 spawn 是一個**無界**迴圈。

    `_generate_inflight` 在這個環裡永遠不會被清（沒服務完就不會發
    `single_image_done`），所以再驅動的條件永遠成立。合成環境實測**每秒約 9100 圈**，
    每一圈都跑一次 nuclear sweep；而它自己 docstring 說的那個 600 秒 backstop 是
    **送出時才觸發**的，所以沒有人再送 `/gen image` 就永遠不會停。

    門檻 N ⇒ 閥門放行 **N-1** 次重試，第 N 次進來就放棄（第一次 spawn 是 pump 做的，
    不算重試）。三個 N 都跑，`>=` 改成 `>` 就會多一次而被抓到；N=1 那格順便釘住
    「1 ＝ 完全不重試」這個有記錄的設定值。
    """
    async def _body():
        spawns = _valve_env(monkeypatch, tmp_path, giveup=giveup)
        proc = FakeProc(2)
        b._webrunner_proc = proc
        await b._reap_oneshot_webrunner(proc)
        settled = await _drain_until(lambda: b._generate_inflight is None)
        return spawns, b._generate_inflight, settled

    spawns, inflight, settled = asyncio.run(_body())
    assert settled, (
        f"閥門沒有讓它停下來（已經 spawn {len(spawns)} 次）——"
        "收屍→再驅動又變回無界迴圈了")
    assert len(spawns) == giveup - 1, (
        f"門檻 {giveup} 應該只放行 {giveup - 1} 次重試，實際 {len(spawns)}")
    assert all(oneshot for _variant, oneshot in spawns), (
        f"重試起來的不是單張伺服器：{spawns}。少了那個旗標，重生出來的行程會把自己"
        "當成批次去跑整條 todo 佇列——使用者只要了一張圖。")
    assert inflight is None, "放棄之後閘門沒有被清掉，佇列會一直卡著"


def test_a_benign_race_does_not_count_as_a_failure(monkeypatch, tmp_path):
    """記帳綁在 `request_id` 上，所以良性競賽不會把健康的請求殺掉。

    良性競賽：server 服務完 R1、閘門前進、pump 剛把 R2 寫上磁碟，然後 server 在它的
    drain poll 撿到 R2 之前 idle 退出。收屍時 inflight 是非 None，但那一輪**是有
    產出的**。

    ⚠️ **這支測試的價值全在「每次都是新的 id」這個序列上。** 兩種記帳方式在「同一筆
    卡住」的序列上**完全分不出來**（實測軌跡都是 `[1,2,3,4,5,6]`、都在第 3 步放棄）；
    只餵那個序列的測試對 keying 一個字都證明不了。只數次數的版本在這個序列上是
    `[1,2,3,4,5,6]`，從第 3 筆**健康**請求起就開始砍人。
    """
    async def _body():
        spawns = _valve_env(monkeypatch, tmp_path)
        # 隔離記帳：讓睡醒後的守衛直接 return，不要連鎖出 spawn。
        b._webrunner_stop_requested = True
        trail = []
        for rid in ("R1", "R2", "R3", "R4", "R5", "R6"):
            b._generate_inflight = rid
            await b._generate_retry_server(rid)
            trail.append(b._generate_oneshot_fails)
        return trail, spawns, b._generate_inflight

    trail, spawns, inflight = asyncio.run(_body())
    assert trail == [1, 1, 1, 1, 1, 1], f"換了 id 卻沒歸零：{trail}"
    assert spawns == [], spawns
    assert inflight == "R6", "健康的請求被放棄掉了"


def test_the_give_up_unjams_the_queue_and_says_so(monkeypatch, tmp_path):
    """放棄時要把四件事一起收乾淨，少任何一件都會留下一種靜默失敗。

    ⚠️ 只斷言 `_generate_inflight is None` 是不夠的：那一條在「拿掉 unlink」的變異
    底下照樣綠，而留著的請求檔會被下一個起來的 server 撿去服務——correlation map 裡
    已經沒有人在等，`_handle_single_image_done` 只記一行 unknown request_id，圖就這樣
    安靜地產出來又消失。佔位訊息那兩條同理：少了 pump 把 handle 存進 `ctx` 那一步，
    編輯就是個 no-op，而使用者的訊息會永遠停在「產圖中…」。
    """
    async def _body():
        _valve_env(monkeypatch, tmp_path)
        req_file = b.SINGLE_IMAGE_REQUEST_FILE
        req_file.write_text("{}", encoding="utf-8")
        inflight_ph = _Placeholder()
        b._single_image_pending["R1"] = {"placeholder": inflight_ph}
        waiting = [_Placeholder(), _Placeholder()]
        b._generate_queue[:] = [
            b._GenerateRequest(f"W{i}", {}, {}, ph)
            for i, ph in enumerate(waiting)]
        await b._generate_give_up_inflight("R1", 3)
        return (b._generate_inflight, "R1" in b._single_image_pending,
                req_file.exists(), list(b._generate_queue),
                inflight_ph.content, [p.content for p in waiting])

    inflight, still_mapped, file_left, queue, inflight_msg, waiting_msgs = \
        asyncio.run(_body())
    assert inflight is None, "閘門沒清，佇列會一直卡著"
    assert not still_mapped, "correlation map 還留著那一筆"
    assert not file_left, "磁碟上的請求檔沒刪——下一個 server 會撿去服務，圖會安靜消失"
    assert queue == [], "佇列沒清空"
    assert inflight_msg and "產圖失敗" in inflight_msg, inflight_msg
    assert all(m and "佇列已清空" in m for m in waiting_msgs), waiting_msgs


def test_a_stop_during_the_backoff_does_not_spawn(monkeypatch, tmp_path):
    """退避可能睡幾十秒，期間 `/stop` 按下去就不該再幫他起 server。

    少了睡醒後那道重新確認，`_spawn_oneshot_webrunner` 會把
    `_webrunner_stop_requested` 清成 False 再 spawn——也就是在使用者明確要求停止之後
    還開一個沒人要的背景程式。

    ⚠️ 只斷言「reaper 沒有 spawn」是不夠的：reaper 早就返回了，spawn 發生在**後來**
    被排上去的那支重試裡。要數整段排空期間的 spawn。
    """
    async def _body():
        spawns = _valve_env(monkeypatch, tmp_path)
        monkeypatch.setattr(b, "_generate_oneshot_backoff", 0.05)
        monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MIN_SEC", 0.05)
        monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MAX_SEC", 0.05)
        task = asyncio.create_task(b._generate_retry_server("R1"))
        await asyncio.sleep(0)
        b._webrunner_stop_requested = True      # 睡覺中途按下 /stop
        await task
        for _ in range(100):
            await asyncio.sleep(0)
        return spawns

    assert asyncio.run(_body()) == [], "使用者按了停止，卻還是起了一個 server"


def test_the_reaper_re_drive_goes_through_the_retry_valve():
    """AST：收屍後的再驅動必須走閥門，不得直接呼叫 `_generate_ensure_server`。

    ⚠️ **這一支單獨證明不了閥門有效**——把閥門的本體改成 `if False and …` 它照樣綠。
    行為那一半是 `test_a_one_shot_that_never_serves_stops_respawning`；兩支都要有，
    互相不能取代。這一支擋的是「有人把 reaper『簡化』回去」。
    用 `_func()` 走 AST 而不是 `getsource()` 做子字串比對：那幾支的 docstring 裡就
    寫著要檢查的名字。
    """
    fn = _func("_reap_oneshot_webrunner")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    # 正面對照：抽取器真的抽到東西了（空集合跟「乾淨」長得一模一樣）。
    assert "_generate_pump" in called, f"抽取器什麼都沒抽到：{sorted(called)}"
    assert "_generate_retry_server" in called, sorted(called)
    assert "_generate_ensure_server" not in called, (
        "收屍又直接重起了，沒有經過閥門——那是無界迴圈")


def test_the_placeholder_survives_promotion_into_inflight(monkeypatch, tmp_path):
    """pump 把一筆提升成 inflight 時，要把佔位訊息的 handle 留在 `ctx` 裡。

    提升之後那個 `_GenerateRequest` 就被丟掉了，只剩 `_generate_inflight` 這個字串。
    少了那一步，放棄重試時就沒有東西可以編輯，使用者的訊息會永遠停在「產圖中…」
    ——那正是這次要移除的症狀之一。

    ⚠️ **這支必須真的走 `_generate_pump`。** 直接把 `ctx["placeholder"]` 塞好的測試
    （例如 `test_the_give_up_unjams_the_queue_and_says_so`）**繞過**了提升那一段，
    所以把 pump 裡那一行刪掉它照樣是綠的。這個缺口是想變異的時候才發現的，不是跑
    測試發現的。
    """
    async def _body():
        _valve_env(monkeypatch, tmp_path, inflight=None)
        ph = _Placeholder()
        ctx = {"channel_id": 2, "message_id": 3, "user_id": 1,
               "submitted_mono": time.monotonic()}
        b._single_image_pending["R9"] = ctx
        b._generate_queue.append(b._GenerateRequest("R9", {}, ctx, ph))
        await b._generate_pump()
        promoted = b._generate_inflight
        mid = ph.content
        await b._generate_give_up_inflight(promoted, 3)
        return promoted, mid, ph.content

    promoted, mid, final = asyncio.run(_body())
    assert promoted == "R9", promoted
    assert mid and "產圖中" in mid, f"pump 沒有把佔位訊息改成產圖中：{mid!r}"
    assert final and "產圖失敗" in final, (
        f"提升成 inflight 之後佔位訊息的 handle 掉了，放棄時編輯不到：{final!r}")


def test_a_backwards_backoff_config_does_not_break_the_valve(monkeypatch,
                                                             tmp_path):
    """`max < min` 的設定（手改 `bot_config.json` 就會有）不得讓閥門整支炸掉。

    `_supervisor.restart_backoff` 在 `maximum < minimum` 或 `minimum <= 0` 時丟
    `ValueError: backoff requires 0 < minimum <= maximum`（實測）。而這支是被
    `_schedule_coro` 排上去的 fire-and-forget，所以那個例外只會變成一行 task 例外
    ——重試不會發生、**放棄也不會發生**，佇列就這樣卡住，正好是這次要移除的症狀。
    所以夾子放在**呼叫點**而不是常數定義處：這樣測試 monkeypatch 那兩個常數之後
    仍然走得到它。
    """
    async def _body():
        _valve_env(monkeypatch, tmp_path, giveup=5)
        monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MIN_SEC", 0.01)
        monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MAX_SEC", 0.001)
        monkeypatch.setattr(b, "_generate_oneshot_backoff", 0.01)
        b._webrunner_stop_requested = True      # 隔離記帳，不要連鎖出 spawn
        await b._generate_retry_server("R1")
        return b._generate_oneshot_fails

    assert asyncio.run(_body()) == 1, "退避設定反過來就把閥門整支炸掉了"


# ===========================================================================
# 四、單張請求的服務看門狗 —— 量「距離最後一則心跳多久」，不是「送出多久」
# ===========================================================================
_T0 = 1_000_000.0   # 合成的單調時鐘；看門狗的判斷只讀測試給的 now／caught_up


def _watchdog_env(monkeypatch, *, alive=True, paused=False, spawned=0.0):
    """換掉看門狗碰真實世界的輸入。

    ⚠️ `_webrunner_liveness` **一定**要換：它讀**磁碟上的** webrunner.pid，而這台
    機器有正式批次在跑，不換的話「死了」那幾支會量到活著。
    """
    box = {"alive": alive}
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (box["alive"], True))
    if paused:
        b.WEBRUNNER_PAUSE_FILE.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(b, "_webrunner_spawned_mono", spawned)
    monkeypatch.setattr(b, "_event_poll_caught_up_mono", None)
    return box


def _pin_dead_grace(monkeypatch) -> float:
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC", 300.0)
    monkeypatch.setattr(b, "GENERATE_ONESHOT_BACKOFF_MAX_SEC", 120.0)
    monkeypatch.setattr(b, "_SINGLE_IMAGE_DEAD_MARGIN_SEC", 600.0)
    return 900.0        # 字面值：兩個退避取大 ＋ 餘裕


def _promote(rid, *, at=_T0, user_id=1):
    ph = FakePlaceholder()
    b._single_image_pending[rid] = {
        "channel_id": 2, "message_id": 3, "user_id": user_id,
        "submitted_mono": at - 5, "serve_seen_mono": at, "placeholder": ph}
    b._generate_inflight = rid
    b.SINGLE_IMAGE_REQUEST_FILE.write_text("{}", encoding="utf-8")
    return ph


def _beat(rid, t, *, within=285.0, phase="generate"):
    b._note_single_image_serving(
        {"ts": 0.0, "type": "single_image_serving", "request_id": rid,
         "in_band": True, "phase": phase, "beat_within_sec": within}, now=t)


def _tick(monkeypatch, t, *, caught_up=None):
    """看門狗的一輪：輪詢器在 `caught_up`（預設＝t）確認讀到底，然後在 t 掃一次。"""
    monkeypatch.setattr(b, "_event_poll_caught_up_mono",
                        t if caught_up is None else caught_up)
    return b._sweep_stale_inflight_serve(now=t)


def test_an_in_band_serve_longer_than_600s_that_keeps_beating_is_not_cancelled(
        monkeypatch):
    """判定條件本身：50 分鐘的帶內服務，每 240 秒一則
    心跳（在 285 秒的承諾之內），整段都不得被取消——而同一刻舊的 600 秒 TTL 早就把它
    掃掉了，所以最後也走一次**真的**送出時掃描。"""
    _watchdog_env(monkeypatch)
    _promote("S1")
    for t in range(0, 3001, 60):
        if t % 240 == 0:
            _beat("S1", _T0 + t)
        assert _tick(monkeypatch, _T0 + t) is None, f"t={t}s 被取消了"
    b._single_image_pending["S1"]["submitted_mono"] = time.monotonic() - 3000
    b._sweep_stale_single_image_state()          # 送出年齡 3000s > TTL
    assert b._generate_inflight == "S1"
    assert "S1" in b._single_image_pending
    assert b.SINGLE_IMAGE_REQUEST_FILE.exists()


def test_a_serve_whose_beats_stop_is_cancelled_after_factor_times_promise(
        monkeypatch):
    """斷掉的服務要在 2 × 承諾之後被收掉，剛好等於不算。上限寫**字面值**：拿
    `b._SINGLE_IMAGE_BEAT_STALE_FACTOR` 算的話，改倍數的變異會兩邊一起跟著動。"""
    async def _body():
        _watchdog_env(monkeypatch)
        ph = _promote("S2", user_id=1)           # 非擁有者 → 泛用句
        pumped: list = []
        monkeypatch.setattr(b, "_generate_pump",
                            lambda: _record_coro(pumped, "pump"))
        _beat("S2", _T0 + 300.0, within=285.0)
        limit = 300.0 + 2 * 285.0                # 870
        before = [_tick(monkeypatch, _T0 + limit - 1),
                  _tick(monkeypatch, _T0 + limit)]
        after = _tick(monkeypatch, _T0 + limit + 1)
        for _ in range(5):
            await asyncio.sleep(0)
        return ph, pumped, before, after

    ph, pumped, before, after = asyncio.run(_body())
    assert before == [None, None]
    assert after == "beat_silence"
    assert b._generate_inflight is None and "S2" not in b._single_image_pending
    assert not b.SINGLE_IMAGE_REQUEST_FILE.exists(), (
        "請求檔要刪，否則下一個 server 會撿去服務")
    assert pumped == ["pump"], "取消之後要推一次佇列"
    assert ph.content and "取消" in ph.content
    assert "beat_silence" not in ph.content and "S2" not in ph.content, (
        "非擁有者看到了內部代碼")


def test_the_owner_sees_why_the_serve_was_cancelled(monkeypatch):
    """擁有者例外走的是單一決策點 `_owner_detail`，所以原因代碼與 request_id 照實給。"""
    async def _body():
        _watchdog_env(monkeypatch)
        ph = _promote("S3", user_id=b.OWNER_USER_ID)
        monkeypatch.setattr(b, "_generate_pump", lambda: _record_coro([], "p"))
        _beat("S3", _T0)
        _tick(monkeypatch, _T0 + 10_000)
        for _ in range(5):
            await asyncio.sleep(0)
        return ph

    ph = asyncio.run(_body())
    assert "beat_silence" in ph.content and "S3" in ph.content


def test_a_request_with_no_beat_while_the_webrunner_is_dead_is_cancelled(
        monkeypatch):
    """監督者放棄（行程死了）而這筆從沒被撿走：寬限過後收掉，剛好等於寬限不算。"""
    grace = _pin_dead_grace(monkeypatch)
    _watchdog_env(monkeypatch, alive=False)
    _promote("D1")
    assert _tick(monkeypatch, _T0) is None
    assert _tick(monkeypatch, _T0 + grace) is None
    assert _tick(monkeypatch, _T0 + grace + 1) == "dead"
    assert b._generate_inflight is None
    assert not b.SINGLE_IMAGE_REQUEST_FILE.exists()


def test_an_alive_webrunner_gets_the_pickup_ceiling_not_the_dead_grace(
        monkeypatch):
    """活著、沒心跳＝在等下一個輪詢點。這也是「舊版背景程式（完全不發心跳）配新 bot」
    的情形：1000 秒（超過舊 TTL 與死亡寬限）不得取消，超過上限才取消。"""
    _watchdog_env(monkeypatch, alive=True)
    _promote("A1")
    assert _tick(monkeypatch, _T0 + 1000) is None
    assert _tick(monkeypatch, _T0 + 3600) is None
    assert _tick(monkeypatch, _T0 + 3601) == "pickup_ceiling"


def test_the_dead_clock_restarts_when_the_webrunner_is_seen_alive_again(
        monkeypatch):
    """死→活→死：空窗要重新起算，否則一次短暫的探測失敗會累積成取消。

    **這裡復活刻意不帶 bot 側 spawn**（`_watchdog_env` 的 `spawned` 留在 0.0）。
    帶了就驗不到東西：`dead_ref` 是 `max(dead_since_mono, _webrunner_spawned_mono)`，
    bot 自己 spawn 過的話那個 `max()` 會**把沒清掉的舊 dead_since 整個遮住**，這支
    測試照樣綠。而復活不經 bot 是真的會發生的——`start_webrunner.py` 那支獨立監督者
    重起時，bot 這邊的 spawn 時刻一動也不動。

    ⚠️ **每一格都要斷言，而且最後要釘 inflight 還在。** 原本這支只斷言最後一格，
    中間三格的回傳值沒人看——於是缺陷版本在 `_T0 + 1000` 那一格就把它取消掉了，
    最後一格因為 `_generate_inflight` 已經是 `None` 而在掃描第一行就 return `None`，
    斷言**因為缺陷而通過**。`None` 這個回傳值分不出「沒東西好取消」與「早就被取消
    了」，所以它一個人撐不起這支測試（樹上變異實測：少了這兩件事那個變異存活）。
    """
    grace = _pin_dead_grace(monkeypatch)
    box = _watchdog_env(monkeypatch, alive=False)
    _promote("D2")
    assert _tick(monkeypatch, _T0) is None
    box["alive"] = True
    assert _tick(monkeypatch, _T0 + 500) is None
    box["alive"] = False
    assert _tick(monkeypatch, _T0 + 1000) is None, (
        "第二次死亡那一格就被收掉了——空窗沒有重新起算。")
    assert _tick(monkeypatch, _T0 + 1000 + grace - 1) is None
    assert b._generate_inflight == "D2", (
        "掃描回 None 不代表沒被取消——也可能是早就取消完了。")


def test_a_bot_side_respawn_restarts_the_dead_clock(monkeypatch):
    """兩次取樣之間 bot 又 spawn 了一次（重起閥門／監督者），然後又死了。從 spawn
    起算，否則取樣剛好都落在死的空窗時會搶掉閥門的工作。"""
    grace = _pin_dead_grace(monkeypatch)
    _watchdog_env(monkeypatch, alive=False)
    _promote("D3")
    _tick(monkeypatch, _T0)
    monkeypatch.setattr(b, "_webrunner_spawned_mono", _T0 + 800)
    assert _tick(monkeypatch, _T0 + 800 + grace) is None
    assert _tick(monkeypatch, _T0 + 800 + grace + 1) == "dead"


def test_a_paused_batch_never_times_out_a_request_it_has_not_picked_up(
        monkeypatch):
    """暫停中的批次完全不 poll 請求檔，所以那段時間不算沉默；解除之後重新給一整個上限。"""
    _watchdog_env(monkeypatch, alive=True, paused=True)
    _promote("P1")
    for t in range(0, 20_401, 600):              # 五個多小時的暫停
        assert _tick(monkeypatch, _T0 + t) is None, f"暫停中 t={t}s 被取消"
    b.WEBRUNNER_PAUSE_FILE.unlink()
    resumed = _T0 + 20_400
    assert _tick(monkeypatch, resumed + 3600) is None
    assert _tick(monkeypatch, resumed + 3601) == "pickup_ceiling"


def test_time_the_event_poller_could_not_read_is_not_counted_as_silence(
        monkeypatch):
    """沉默的終點是輪詢器最後一次「確認讀到底」的時刻。從沒確認過就不判；卡在
    「快取沒就緒」的那段也不算。"""
    _watchdog_env(monkeypatch)
    _promote("N1")
    _beat("N1", _T0)
    assert b._sweep_stale_inflight_serve(now=_T0 + 99_999) is None, (
        "輪詢器從沒確認過讀到底，卻判了沉默")
    for t in (600, 3000, 20_000):                # 輪詢器卡在「快取沒就緒」
        assert _tick(monkeypatch, _T0 + t, caught_up=_T0) is None
    assert _tick(monkeypatch, _T0 + 20_000) == "beat_silence"


def test_the_poller_stamps_caught_up_only_when_nothing_is_left_unread(
        monkeypatch, tmp_path):
    """蓋章的三個出口都要是「確定沒有未讀位元組」；快取沒就緒那個出口不得蓋章。"""
    events = tmp_path / "events.ndjson"
    events.write_text('{"type":"x"}\n', encoding="utf-8", newline="\n")
    monkeypatch.setattr(b, "EVENTS_FILE", events)
    monkeypatch.setattr(b, "_event_offset", 0)
    monkeypatch.setattr(b, "_event_poll_caught_up_mono", None)

    class _NotReady:
        user = None

        def get_channel(self, _cid):
            return None

    monkeypatch.setattr(b, "client", _NotReady())
    asyncio.run(b._poll_events_once())
    assert b._event_poll_caught_up_mono is None, (
        "快取沒就緒、事件還沒讀，卻蓋了「讀到底」的章")
    assert b._event_offset == 0

    chan = FakeChannel()

    class _Ready:
        user = object()

        def get_channel(self, _cid):
            return chan

    monkeypatch.setattr(b, "client", _Ready())
    before = time.monotonic()
    asyncio.run(b._poll_events_once())
    stamped = b._event_poll_caught_up_mono
    assert stamped is not None and before <= stamped <= time.monotonic()
    assert b._event_offset == events.stat().st_size
    monkeypatch.setattr(b, "_event_poll_caught_up_mono", None)
    asyncio.run(b._poll_events_once())          # size <= offset 那個出口
    assert b._event_poll_caught_up_mono is not None


def test_skipped_events_refresh_the_last_evidence(monkeypatch):
    """bot 自己跳過一段事件（offset 拉到檔尾）之後，那段不能算成沉默。"""
    _watchdog_env(monkeypatch)
    _promote("K1")
    _beat("K1", _T0)
    b._single_image_events_skipped(now=_T0 + 5000)
    assert _tick(monkeypatch, _T0 + 5000 + 570) is None
    assert _tick(monkeypatch, _T0 + 5000 + 571) == "beat_silence"


def test_every_offset_jump_tells_the_watchdog():
    """範圍守門：任何把事件 offset 拉到檔尾的地方都要通知看門狗。"""
    for name in ("on_ready", "_spawn_webrunner"):
        called = {ast.unparse(n.func) for n in ast.walk(_func(name))
                  if isinstance(n, ast.Call)}
        assert "_single_image_events_skipped" in called, (
            f"`{name}` 會把事件 offset 拉到檔尾，卻沒告訴看門狗那段是跳過的")


def test_every_bot_side_spawn_restarts_the_dead_clock():
    """`_spawn_webrunner` 一定要蓋 spawn 時刻，而且要用單調時鐘。"""
    fn = _func("_spawn_webrunner")
    declared = {n for node in ast.walk(fn) if isinstance(node, ast.Global)
                for n in node.names}
    stamps = [node for node in ast.walk(fn) if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "_webrunner_spawned_mono"
                      for t in node.targets)]
    assert "_webrunner_spawned_mono" in declared
    assert stamps and all(isinstance(s.value, ast.Call)
                          and ast.unparse(s.value.func) == "time.monotonic"
                          for s in stamps)


def test_a_beat_for_a_request_that_is_not_inflight_is_ignored(monkeypatch):
    """佇列裡的、不存在的 request_id 的心跳一律忽略——它們不是正在被服務的那一筆。"""
    _watchdog_env(monkeypatch)
    _promote("I1")
    _submit("QUEUED", queued=True)
    _beat("QUEUED", _T0 + 10)
    _beat("nobody", _T0 + 10)
    assert "serve_within" not in b._single_image_pending["QUEUED"]
    assert "nobody" not in b._single_image_pending


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), 0, -5, True])
def test_a_malformed_promise_still_counts_as_life_but_not_as_a_promise(
        monkeypatch, bad):
    """壞掉的承諾不得被當成承諾用（會算出荒謬的容許量），但那一則心跳本身仍算「有人
    在服務」，所以退回寬上限。"""
    _watchdog_env(monkeypatch)
    _promote("M1")
    _beat("M1", _T0, within=bad)
    assert b._single_image_pending["M1"]["serve_within"] is None
    assert _tick(monkeypatch, _T0 + 3600) is None
    assert _tick(monkeypatch, _T0 + 3601) == "beat_silence"


def test_queue_time_is_not_counted_against_the_pickup_ceiling(monkeypatch):
    """在佇列裡等前面幾張的時間不是這一筆的沉默：起點是「寫上磁碟的那一刻」。"""
    async def _body():
        _watchdog_env(monkeypatch)
        monkeypatch.setattr(b, "_generate_ensure_server",
                            lambda: _record_coro([], "ensure"))
        monkeypatch.setattr(b, "_generate_refresh_queue",
                            lambda: _record_coro([], "refresh"))
        _submit("LATE", age_sec=7200, queued=True)   # 在佇列裡等了兩小時
        await b._generate_pump()
        return b._single_image_pending["LATE"].get("serve_seen_mono",
                                                   time.monotonic())

    promoted_at = asyncio.run(_body())
    assert b._generate_inflight == "LATE"
    assert _tick(monkeypatch, promoted_at + 60) is None


def test_the_dispatcher_feeds_beats_to_the_watchdog(monkeypatch):
    """派發分支要真的把心跳交給看門狗，而且**不得送任何訊息**。"""
    _watchdog_env(monkeypatch)
    _promote("R9")
    chan = FakeChannel()
    asyncio.run(b._handle_event(chan, {
        "ts": 0.0, "type": "single_image_serving", "request_id": "R9",
        "in_band": False, "phase": "start", "beat_within_sec": 120.0}))
    assert b._single_image_pending["R9"]["serve_within"] == 120.0
    assert chan.sent == []


def test_the_health_loop_actually_cancels_a_dead_serve(monkeypatch):
    """行為守門：看門狗真的掛在每分鐘的迴圈上，而且在 `enabled` 閘**外面**（報告關著
    也要跑）。AST 看得到「有人寫了那一行」，看不到「那一行在哪一格縮排」。"""
    _watchdog_env(monkeypatch)
    monkeypatch.setattr(b, "_rotate_periodic_ndjson_logs", lambda: None)
    monkeypatch.setattr(b, "_check_code_drift", lambda: _record_coro([], "d"))
    monkeypatch.setattr(b, "DAILY_HEALTH_REPORT", {"enabled": False})
    # 同一條迴圈也掛著每日的後端模型目錄檢查，那個會起真正的 CLI 子行程。
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK", {"enabled": False})
    monkeypatch.setattr(b, "_generate_pump", lambda: _record_coro([], "p"))
    now = time.monotonic()
    _promote("H1", at=now - 10_000)
    _beat("H1", now - 10_000)
    monkeypatch.setattr(b, "_event_poll_caught_up_mono", now)

    async def _one_tick():
        try:
            await asyncio.wait_for(b._daily_health_loop(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    asyncio.run(_one_tick())
    assert b._generate_inflight is None
