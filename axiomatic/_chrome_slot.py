"""_chrome_slot.py — 跨行程的「單一 Chrome 槽」諮詢鎖（advisory lock）。

bot（``discord_bot.py``）與獨立的瀏覽器驗證器（``verify_browser.py``）都會在同一台
機器上開 Chrome。系統長久以來的硬性不變量是「同時只有一個 Chrome stack 在跑」
（對應 ``_webrunner_alive()``）：正式 webrunner 與隔離的驗證瀏覽器絕不可重疊，否則
(a) bot 的 nuclear sweep（無條件殺光所有 ``chrome.exe`` / ``chromedriver.exe``）會在
驗證跑到一半時誤殺它；(b) 驗證在 webrunner 還活著時開瀏覽器，等於同一個槽被雙重佔用、
搶同一份登入 profile。

本模組是一個 **被動的共用輔助模組**——與 ``_batch_config`` / ``_bot_config`` /
``_run_progress`` 同屬「允許的第三方通道」，**不是** bot↔webrunner 的直接 import。
只用標準庫；``psutil`` 若可 import 就拿來判行程存活。缺了 psutil 時：POSIX 退回
``os.kill(pid, 0)`` 探測，**Windows 則完全不探測**（見 `_pid_alive` 的說明——Windows 上
``os.kill(pid, 0)`` 會送出真正的 Ctrl+C，不是探測），一律保守視為活著、由時間 staleness
兜底。

協定（諮詢式；單機、低競爭）：
  * 鎖就是 repo root 下的一個檔 ``chrome_slot.lock``，用 ``O_CREAT|O_EXCL`` 原子建立。
    內容是 JSON：``{"pid", "owner", "label", "acquired_at"}``。
  * 持有者若 **行程已死** 或 **持有超過 ``stale_after`` 秒**，即視為 stale、可被搶走。
    時間 backstop 是在「持有者沒 release 就死掉、又無法判定行程存活」時，限制最長卡死時間。
  * 搶佔（steal）走第二把 ``O_CREAT|O_EXCL`` 的 marker ``chrome_slot.steal.lock``，並在
    刪舊鎖之前重讀一次確認持有者沒換人。搶佔是三步驟、不是原子操作；少了這兩層，兩個
    行程會同時判定 stale、後到的那個把先到的那個**剛建好的活鎖**刪掉，兩邊都拿到槽
    （2026-08-30 實測重現）。
  * ``try_acquire()`` 做 **一次非阻塞** 嘗試（給 bot 的 async 輪詢迴圈用）。
    ``acquire()`` 會 sleep 阻塞、最多等 ``timeout``（給獨立腳本用）。
  * ``release()`` 只在「這個行程仍持有」時才刪檔。

呼叫端必須遵守的順序契約（本模組只提供互斥；「誰讓誰」的政策在呼叫端）：
  * **驗證端**：``try_acquire`` 成功後，先檢查 ``webrunner.pid`` 是否有活著的 webrunner；
    若有 → ``release`` 並讓位（不要開瀏覽器）。否則開瀏覽器、跑完、在 ``finally`` 裡 release。
  * **spawn 端（bot 與 ``start_webrunner.py``）**：在任何 nuclear sweep ＋ webrunner spawn
    之前，先取得這個槽（bot 用 async 輪詢、
    啟動器用阻塞的 ``acquire``，都有 timeout）；持鎖期間做 sweep ＋ spawn ＋ 寫
    ``webrunner.pid``；寫完 pid 就 release（或失敗時 release）。**pid 一定要在 release
    之前寫**——反過來會留下「槽空了但 pid 還沒寫」的空窗，驗證端剛好在那一瞬間取槽
    就會判定沒人在跑。
    這是 **短臨界區**，不是整個 webrunner 生命週期都握著。webrunner 活著的期間，驗證端會靠
    ``webrunner.pid`` 檢查讓位，所以 bot 不需要一直握著鎖。
  * **webrunner 自己（2026-09-11 起）**：它**不取槽**，但在 ``main()`` 之前會檢查
    ``webrunner.pid``——沒有人認領（檔案不存在，或裡面那個 pid 已經死了）就寫自己的
    pid 進去，收尾時只刪「還記著自己那一筆」的檔
    （``_webrunner_shared.claim_liveness_signal`` / ``release_liveness_signal``）。
    這是為了**裸跑**：直接 ``py -3 axiomatic/webrunner_novelai.py`` 沒有父行程，
    上面那兩個訊號一個都不會有，於是驗證端會開出自己的瀏覽器、``start_webrunner.py``
    也會再起第二個批次。上面那條「pid 一定要在 release 之前寫」不受影響——父行程
    先寫到的話，webrunner 這一層就是 no-op。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:  # 行程存活判定用；缺了不致命，退回 os.kill / 時間 staleness。
    import psutil  # type: ignore
except Exception:  # pylint: disable=broad-except
    psutil = None  # type: ignore

# 與其他共用模組一致：parent.parent ＝ repo root（webrunner.pid / .chrome_profile 都在此）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = _PROJECT_ROOT / "chrome_slot.lock"

# 安全網預設：驗證腳本自己的總時限約 240s，bot 的臨界區只有 spawn 那幾秒；600s 的時間
# staleness 遠大於兩者，足以在「持有者死掉又無法判定存活」時自動回收，又不會誤搶一個還在
# 正常跑的持有者。呼叫端可覆寫。
DEFAULT_STALE_AFTER_SEC = 600.0

# 搶佔（steal）用的第二把鎖。見 `_steal` 的說明：搶一個 stale 的持有者是「讀了再刪再建」
# 的三步驟，不做序列化的話兩個行程可以同時判定 stale、後到的那個會把先到的那個**剛建好
# 的、活的**鎖檔刪掉，兩邊都以為自己拿到槽。臨界區只有幾毫秒，所以 marker 的過期時間
# 遠短於主鎖。
STEAL_MARKER_PATH = _PROJECT_ROOT / "chrome_slot.steal.lock"
STEAL_MARKER_STALE_SEC = 30.0


def _pid_alive(pid: int | None) -> bool:
    """盡力判定 pid 是否還活著。無法判定時回 True（保守：不要誤搶）。"""
    if not pid or pid <= 0:
        return False
    if psutil is not None:
        try:
            return psutil.pid_exists(int(pid))
        except Exception:  # pylint: disable=broad-except
            return True
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) **不是**探測：signal.CTRL_C_EVENT == 0，CPython 會把
        # signal 0 導向 GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)——把 pid 當 console
        # process group id、對整組送出真正的 Ctrl+C（或直接失敗丟 OSError）。沒有 psutil
        # 時寧可不探測：回 True 保守視為活著，staleness 交給時間 backstop
        # （DEFAULT_STALE_AFTER_SEC）判定，最多多等 10 分鐘，不會誤傷任何行程。
        return True
    try:
        os.kill(int(pid), 0)  # POSIX：不送訊號、只探測存在性
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但無權限 → 視為活著
    except OSError:
        return True


def read_holder() -> dict | None:
    """讀目前持有者的 metadata。檔不存在回 None。存在但內容半截/壞掉（例如剛 create 還沒
    write 的空窗）→ 回一個以檔案 mtime 當 ``acquired_at``、``pid`` 為 None 的 dict，讓
    staleness 仍可靠時間判定，不會把「正在被別人建立」誤判成不存在。"""
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    # **讀不出來 ≠ 沒有人持有。** 不能往外拋（這支在取槽的路徑上，拋出去等於一次
    # 半寫入的鎖檔就讓 spawn 整個炸掉），但回 `None` 也不對——`None` 在這個模組裡
    # 的意思是「檔案不存在」，而這裡檔案**存在**。
    #
    # 差別是實質的。回 `None` 時 `acquire()` 會走「create 與 read 之間檔案消失了」
    # 那條路，再試一次 `O_EXCL` 建立——而檔案還在，所以必然失敗，`acquire()` 回
    # False。下一次呼叫再走一遍同樣的路。**於是槽被永久卡住**，而且
    # `held_by_live_other()` 同時回 False，連「忙線中」都不會說，要有人手動去刪
    # 那個檔案才解得開。
    #
    # 改成跟「空檔／JSON 壞掉」同一條路（`_holder_from_mtime`）之後，staleness 由
    # 時間 backstop 判定：`DEFAULT_STALE_AFTER_SEC` 一到就被搶走，自己會好。這也
    # 正是這個函式 docstring 從一開始就寫的行為（「存在但內容半截／壞掉 → 回一個
    # 以檔案 mtime 當 `acquired_at` 的 dict」）——一個解不開的檔案就是內容壞掉。
    #
    # 「檔案真的消失了」那條路完全沒有變：`FileNotFoundError` 仍然回 `None`，
    # `acquire()` 的重試競態照舊。
    #
    # 殘留風險（已知、可接受）：若連 `stat()` 都失敗，`_holder_from_mtime` 會用
    # `time.time()`，於是永遠不 stale——那跟改之前的結果一樣糟，不是新的退步。
    except (OSError, UnicodeDecodeError):
        return _holder_from_mtime()
    raw = raw.strip()
    if not raw:
        return _holder_from_mtime()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:  # pylint: disable=broad-except
        pass
    return _holder_from_mtime()


def _holder_from_mtime() -> dict:
    try:
        mtime = LOCK_PATH.stat().st_mtime
    except OSError:
        mtime = time.time()
    return {"pid": None, "owner": "?", "label": "?", "acquired_at": mtime}


def _is_stale(holder: dict, stale_after: float) -> bool:
    pid = holder.get("pid")
    if pid is not None and not _pid_alive(pid):
        return True
    started = holder.get("acquired_at")
    if not isinstance(started, (int, float)):
        return False
    return (time.time() - float(started)) > float(stale_after)


def _write_meta(fd: int, owner: str, label: str) -> None:
    meta = {
        "pid": os.getpid(),
        "owner": owner,
        "label": label,
        "acquired_at": time.time(),
    }
    os.write(fd, json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def _create_exclusive(owner: str, label: str) -> bool:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        _write_meta(fd, owner, label)
    finally:
        os.close(fd)
    return True


def _same_holder(a: dict, b: dict) -> bool:
    """兩份 metadata 是否指向同一次持有。`acquired_at` 是搶佔判定的關鍵——同一個 pid
    重新取得一次鎖也算換了持有者，因為那份 stale 判定已經不適用了。"""
    return (a.get("pid") == b.get("pid")
            and a.get("acquired_at") == b.get("acquired_at"))


def _clear_stale_steal_marker() -> None:
    """marker 過期就清掉（前一個搶佔者在臨界區裡死了）。**清掉的人不會在同一輪接著搶**
    ——不然兩個行程可以同時清、同時搶，就變回這把 marker 要防的那個 bug。清掉之後大家
    回到 `O_CREAT|O_EXCL`，由 kernel 決定誰是唯一的搶佔者。"""
    try:
        age = time.time() - STEAL_MARKER_PATH.stat().st_mtime
    except OSError:
        return
    if age <= STEAL_MARKER_STALE_SEC:
        return
    try:
        os.unlink(str(STEAL_MARKER_PATH))
    except OSError:
        pass


def _steal(holder: dict, owner: str, label: str) -> bool:
    """把一個已判定 stale 的持有者搶過來。取得回 True。

    搶佔不是原子操作（要「確認 stale → 刪掉舊的 → 建立新的」三步），所以先用第二把
    `O_CREAT|O_EXCL` 的 marker 把整段序列化：同一時間只有一個行程能進來。進來之後**重讀
    一次**主鎖，確認它還是我們剛剛判定 stale 的那一份；若已經換人（別人搶成功、或有人
    正常取得），就放手回 False——那是一把新的、活的鎖，不可以刪。

    沒有這兩層，兩個行程會同時判定 stale、同時 unlink、同時 create，**後到的那個會刪掉
    先到的那個剛建好的鎖**，兩邊都拿到槽。那正是本模組存在的理由被推翻的情形：bot 的
    nuclear sweep 會在驗證瀏覽器跑到一半時把它殺掉。"""
    try:
        fd = os.open(str(STEAL_MARKER_PATH),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        _clear_stale_steal_marker()
        return False
    except OSError:
        return False
    try:
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "owner": owner,
                                     "at": time.time()},
                                    ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        current = read_holder()
        if current is None:
            return _create_exclusive(owner, label)
        if not _same_holder(current, holder):
            return False
        try:
            os.unlink(str(LOCK_PATH))
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return _create_exclusive(owner, label)
    finally:
        try:
            os.unlink(str(STEAL_MARKER_PATH))
        except OSError:
            pass


def try_acquire(owner: str, *, stale_after: float = DEFAULT_STALE_AFTER_SEC,
                label: str = "") -> bool:
    """做一次非阻塞嘗試。取得回 True，被別人持有回 False。

    若同一個行程已持有（pid 相同）→ 視為已取得回 True（idempotent）。
    若現任持有者 stale（行程死了或超時）→ 嘗試搶走。"""
    if _create_exclusive(owner, label):
        return True
    holder = read_holder()
    if holder is None:
        # create 與 read 之間檔案消失了 → 再試一次原子建立。
        return _create_exclusive(owner, label)
    if holder.get("pid") == os.getpid():
        return True  # 本行程已持有
    if _is_stale(holder, stale_after):
        return _steal(holder, owner, label)
    return False


def acquire(owner: str, *, stale_after: float = DEFAULT_STALE_AFTER_SEC,
            timeout: float = 0.0, poll: float = 0.5, label: str = "") -> bool:
    """阻塞版（給獨立腳本用）：輪詢 ``try_acquire`` 直到取得或超過 ``timeout`` 秒。
    ``timeout`` <= 0 表示只試一次。會 sleep，請勿在 async event loop 裡用（bot 端請自己用
    ``try_acquire`` ＋ ``asyncio.sleep`` 輪詢，才不會卡住 event loop）。

    **截止時刻用 `time.monotonic()`，不是 `time.time()`。** 後者可被調整（NTP 的 step 修正、
    手動改時鐘、虛擬機快照還原；**換時區與日光節約時間不會**——`time.time()` 回的是
    UTC epoch 秒），而這裡等的是一段**間隔**：往回撥一小時會讓一次 45
    秒的等待變成一小時（`verify_browser` 就這樣掛在那裡不動），往前撥則讓
    ``timeout`` 完全失效、退化成只試一次。這個值不離開本行程，所以直接換是安全的
    ——寫進檔案或要跟別的行程比對的時間點才必須留在 `time.time()`
    （例如鎖檔裡的 ``acquired_at``，`_holder_from_mtime` 拿它跟檔案 mtime 比）。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if try_acquire(owner, stale_after=stale_after, label=label):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, poll))


def release(owner: str) -> None:
    """只在本行程仍持有時刪鎖檔。永不 raise。"""
    holder = read_holder()
    if holder is None:
        return
    if holder.get("pid") == os.getpid():
        try:
            os.unlink(str(LOCK_PATH))
        except FileNotFoundError:
            pass
        except OSError:
            pass


def held_by_live_other(owner: str | None = None) -> bool:
    """是否有「別的、還活著的」行程正持有這個槽（給診斷／『忙線中』訊息用）。
    本行程自己持有不算。stale 的持有者也不算（因為可被搶）。"""
    holder = read_holder()
    if holder is None:
        return False
    if holder.get("pid") == os.getpid():
        return False
    return not _is_stale(holder, DEFAULT_STALE_AFTER_SEC)
