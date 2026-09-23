"""Helpers shared by process supervisor entry points.

`restart_backoff` and `child_exit_is_fatal` are pure functions.
`acquire_single_instance_lock` touches the filesystem (it has to — an OS-held
lock is the only staleness-free way to answer "is another instance already
running?").

子行程輸出的落地（`stream_child` / `trim_log` / `say` 一組）也收在這裡：兩支
啟動器都要把子行程的 stdout+stderr 同時送到主控台與記錄檔，實作只該有一份。
"""
from __future__ import annotations

import errno
import os
import subprocess  # nosec B404 — 監督者就是在起子行程
import sys
import threading
import time


# 子行程用這個 rc 說「不是我壞了，是**已經有另一個實例在跑**」。
#
# 為什麼需要一個專用 rc：supervisor 的重啟迴圈原本只有兩種放棄條件，兩種都擋不到
# 這一類失敗。退避只是把重試拉慢，rapid-fail giveup 看的是「跑多久」——而被鎖擋掉
# 的子行程每次都在一秒內**乾淨地**結束，重試一百次也不會變成功，中間每一輪還照印
# 一行「restarting in Ns」。所以要一個 rc 讓 supervisor 直接分辨「重試沒有意義」。
#
# 為什麼是 3：0 是正常結束、1 是未捕捉例外與一般失敗、2 是 CPython 自己的命令列
# 錯誤（`python 不存在的檔案.py`）。3 是第一個沒有被佔用的值。**不要改成 1**：
# 那會跟真的崩潰混在一起，supervisor 就分不出來了。
RC_ALREADY_RUNNING = 3

# 子行程用這個 rc 說「**設定還沒填**」——憑證檔或 `bot_config.json` 不存在／還是
# 範本的原樣。與 `RC_ALREADY_RUNNING` 同一類：重試一百次也不會變成功，而且每一輪
# 都會再印一次同樣的抱怨。全新 clone 第一次啟動一定會走到這條路，所以它必須是一
# 句看得懂的話加上一個乾淨的結束，不是 traceback 加上無限重生。
#
# **這一個刻意跨兩套 rc 契約**（bot 的與 webrunner 的），所以取的是兩邊都還沒用到
# 的 5：bot 用掉 0–3，webrunner 用掉 0–4。兩邊問的是同一個問題（「這台機器上還沒
# 有可用的設定」），答案也一樣（停下來等人），沒有理由給它兩個不同的數字。
RC_SETUP_INCOMPLETE = 5

# ---- webrunner 專用的 rc 契約（與上面那個 bot 用的 rc 互不相干）------------
# 兩支監督者（`start_webrunner.py` 與 bot 的 `_watch_for_fallback`）都用這裡的
# 值判斷「要不要重生」，收在同一處避免兩邊漂移。
#
# 3 = **零產出**：這一輪嘗試過生成、卻一張都沒存（走完整輪的 zero-save backstop，
#     以及「連續失敗到 abort 門檻」的中途放棄，都回這個值）。重生**一次**是對的
#     ——壞掉的是這個 session。但連續好幾輪都零產出，就代表重生解決不了，該停。
#     注意這個值與 `RC_ALREADY_RUNNING` 同為 3 是巧合、互不影響：那個是 bot 本體
#     對 bot 啟動器說的話，這個是 webrunner 對 webrunner 監督者說的話，兩條線
#     沒有交集。**不要**把 `child_exit_is_fatal` 拿來判 webrunner 的 rc。
# 4 = **被擋住**：站方跳出購買／方案資訊，重試不會有結果。重生一次都嫌多。
RC_ZERO_PROGRESS = 3
RC_GENERATION_BLOCKED = 4


def webrunner_exit_needs_human(rc: int) -> bool:
    """webrunner 結束後：True = 不要重生，直接停下來等人處理。

    判準與 `child_exit_is_fatal` 一樣是「重試會不會有機會成功」，但對象不同
    （webrunner 子行程 vs bot 子行程），所以刻意是兩個函式。
    """
    return rc in (RC_GENERATION_BLOCKED, RC_SETUP_INCOMPLETE)


def child_exit_is_fatal(rc: int) -> bool:
    """子行程結束後：True = supervisor 不要重試，直接收工。

    兩種：「已經有另一個實例在跑」與「設定還沒填」。判準是**重試會不會有機會
    成功**，不是「錯誤嚴不嚴重」：憑證過期、設定值寫錯這些重試也不會成功，但它們
    的 rc 跟真正的崩潰無法區分，只能靠 rapid-fail giveup 兜著。缺檔案這一種分得
    出來，所以它有自己的 rc。
    """
    return rc in (RC_ALREADY_RUNNING, RC_SETUP_INCOMPLETE)


def restart_backoff(
    current: float,
    *,
    minimum: float,
    maximum: float,
    healthy: bool,
) -> tuple[float, float]:
    """Return ``(wait_now, next_backoff)`` for a failed child process.

    A healthy run resets immediately. A short-lived failure waits for the
    current delay and only then increases the delay for the following retry.
    """
    if minimum <= 0 or maximum < minimum:
        raise ValueError("backoff requires 0 < minimum <= maximum")
    wait_now = minimum if healthy else min(max(current, minimum), maximum)
    next_backoff = minimum if healthy else min(wait_now * 2, maximum)
    return wait_now, next_backoff


class InstanceLock:
    """持有中的單一實例鎖。呼叫端**應該**把它保留到行程結束。

    **這個類別刻意沒有 `__del__`，也不得有。** 實測（Windows 11 / CPython）：
    `hasattr(InstanceLock, "__del__")` 是 False，而丟掉最後一個參照再 `gc.collect()`
    之後，第二次 `acquire_single_instance_lock` 仍然回 `None`——**鎖還在**。因為
    鎖掛在 open file description 上，沒有人關 fd 就沒有人放鎖：物件被回收時 fd
    洩漏，鎖一路被持有到行程結束，互斥仍然成立。

    這是刻意選的安全方向，**不要「把 `__del__` 補完」**：多洩漏一個 fd 沒有人會
    受傷（一個行程一把，行程結束時 OS 全收），靜默放掉鎖則會讓第二個實例起得
    來——兩個批次監督者搶同一份 `.chrome_profile/`、各自 nuclear sweep 把對方的
    Chrome 殺掉，而兩邊的記錄看起來都正常。`test_supervisor` 有兩支在守這條
    （`test_instance_lock_must_not_grow_a_del_method` 釘屬性、
    `test_dropping_the_reference_does_not_release_the_lock` 釘行為）。

    這跟 `acquire_single_instance_lock` 那條「判斷不出來就往照常啟動倒」是**兩件
    不同的事**，別混在一起講：那條講的是**取不到鎖時**往哪邊倒（往放行），這條
    講的是**已經拿到鎖之後**參照消失怎麼辦（維持持有）。

    `degraded=True` 代表「鎖機制本身不可用」（見
    `acquire_single_instance_lock` 的說明），此時它只是個空殼，不保證互斥。
    """

    __slots__ = ("_fd", "path", "degraded")

    def __init__(self, fd: int | None, path: str, degraded: bool = False):
        self._fd = fd
        self.path = path
        self.degraded = degraded

    def release(self) -> None:
        """放掉鎖——**只是禮貌性收尾**，不是正確性的一部分。

        行程無論正常結束、未捕捉例外還是被 `taskkill /F` 砍掉，OS 都會關掉 fd
        並連帶放掉鎖，所以漏呼叫它不會留下一把誰也解不開的殘留鎖。刻意做成呼叫
        幾次都不炸：`finally` 裡的那次可能已經釋放過，而 `degraded` 的空殼
        （`fd is None`）根本沒有 fd 可關。
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.close(fd)
        except OSError:
            pass


# 「這個檔案已經被別人鎖住了」專用的 errno。**只有這幾個**代表「另一個實例正在
# 跑」；其他任何 errno 代表的是「這台機器上的鎖機制有問題」，那是完全不同的一件事
# ——把兩者混成同一個答案，會讓一個與併發無關的問題長得跟「已經有實例在跑」一模一
# 樣，然後啟動器永遠拒絕啟動（見 `acquire_single_instance_lock` 的說明）。
#
# 兩個平台給的答案不一樣，所以**兩邊都要收**（本機實測，2026-09-08）：
#   * Windows `msvcrt.locking(fd, LK_NBLCK, 1)` 對已鎖住的區段給 **EACCES(13)**。
#     阻塞版 `LK_LOCK` 重試失敗給 **EDEADLOCK(36)**——我們用的是非阻塞版，收著純粹
#     是保險（哪天有人改成阻塞版，至少不會被誤判成「鎖壞了」）。
#   * POSIX `flock(fd, LOCK_EX | LOCK_NB)` 對已持有的檔案給 **EWOULDBLOCK/EAGAIN**。
#
# `EWOULDBLOCK` 在 Linux 上就是 `EAGAIN`（都是 11），**但在 Windows 的 CPython 上
# 不是**：實測 `errno.EAGAIN == 11` 而 `errno.EWOULDBLOCK == 10035`（Winsock 的
# WSAEWOULDBLOCK）。所以不能只寫其中一個，也不能假設兩者相等。
# 用 `getattr` 取值是因為這幾個名字不保證每個平台都有；取不到就當它不存在。
_LOCK_HELD_ERRNOS = frozenset(
    value for value in (
        getattr(errno, _name, None)
        for _name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK", "EDEADLOCK")
    ) if value is not None
)


def acquire_single_instance_lock(path) -> InstanceLock | None:
    """對 `path` 取得行程生命週期內的獨佔鎖，用來擋掉「同一支程式被啟動兩次」。
    取得 → 回 `InstanceLock`；**已有其他實例持有 → 回 None**。

    **一支程式一個鎖檔**：啟動器鎖 `.discord_bot_supervisor.lock`、bot 本體鎖
    `.discord_bot.lock`，刻意分開。共用同一個檔案的話，啟動器會把自己 spawn 出來
    的 bot 擋掉——那是啟動器唯一該放行的子行程。

    用 OS 層的檔案鎖而不是 pid 檔，是因為 pid 檔有兩個治不好的毛病：行程被硬砍
    時檔案會殘留（下次永遠拒絕啟動），而 PID 又會被系統回收再指派（殘留的 pid
    剛好對上不相干的行程，一樣永遠拒絕啟動）。OS 鎖在行程消失的當下就自動釋放，
    連 `taskkill /F` 也一樣，沒有殘留這回事。

    **「判斷不出來」時往哪邊倒**：倒向「照常啟動」（回一個 `degraded` 的殼），
    不是倒向「拒絕啟動」。這跟 CLAUDE.md 那條 PID 存活探測的保守方向相反，是
    刻意的——那條規則守的是「別開出第二套 Chrome」，代價對稱；這裡兩種錯誤的
    代價不對稱：判錯成「拒絕」會讓 bot 因為一個無關的檔案系統問題**完全不啟動**
    且沒人會發現，判錯成「放行」最多退回加這道鎖之前的狀態（重複實例）。

    **三種結果，靠 errno 分**（2026-09-08 修；在那之前只有兩種，任何 `OSError`
    都算「已有實例」，等於把上面那條政策寫反了）：

    | 結果 | 意思 | 回傳 |
    |---|---|---|
    | 鎖到了 | 只有我在跑 | `InstanceLock`（`degraded=False`）|
    | `_LOCK_HELD_ERRNOS` | 另一個實例正在跑 | `None` |
    | 其他 errno／沒有 `msvcrt`&`fcntl`／開不了檔 | **判斷不出來** | `InstanceLock(degraded=True)` |

    `degraded=True` 是「我沒有提供互斥保護」的誠實回報，**呼叫端有責任講出來**：
    兩支啟動器都會 `say()` 一行。不然放行就是無聲的，而無聲的降級跟沒有這把鎖
    是同一件事。

    子行程不會繼承這把鎖：Python 3.4+ 起 fd 預設 non-inheritable（PEP 446），
    所以 launcher 底下 spawn 出來的 bot 不會把鎖一起帶走。
    """
    path = str(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as error:
        print(f"single-instance lock unavailable ({path}): {error!r}",
              file=sys.stderr)
        return InstanceLock(None, path, degraded=True)

    try:
        if os.name == "nt":
            import msvcrt
            # LK_NBLCK：非阻塞，鎖目前位置起算 1 byte。已被別人鎖住就丟
            # OSError，不會卡住。
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _LOCK_HELD_ERRNOS:
            # 被別的實例鎖住了——這是本函式唯一會回 None 的路徑。
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        # 「鎖不動」不等於「有人持有」。走到這裡代表鎖呼叫本身壞了（EBADF、
        # EINVAL、不支援檔案鎖的網路磁碟給的 ENOLCK……），也就是**判斷不出來**，
        # 依本函式上面寫明的政策倒向「照常啟動」。
        #
        # **這一行改掉了一個安全機制的失效方向，是刻意的**（2026-09-08）：改之前
        # 任何 `OSError` 都回 `None`，於是一個與併發完全無關的檔案系統問題會被講成
        # 「已經有另一個實例在執行」，啟動器從此**永遠拒絕啟動**，而訊息還指著一個
        # 不存在的實例——沒有人查得出來。改之後未知 errno 會放行，代價是可能出現
        # 重複實例。選後者是因為這個函式的政策本來就寫著往「照常啟動」倒，現在只是
        # 讓程式碼跟它一致；而且放行**不再是無聲的**：`degraded` 會被兩支啟動器印
        # 出來，stderr 也會留下 errno。
        print(f"single-instance lock check failed (errno={error.errno}): "
              f"{error!r}; 照常啟動，但這次沒有互斥保護",
              file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)
    except ImportError as error:
        # 平台沒有 msvcrt/fcntl（極罕見）。同樣往「照常啟動」倒。
        print(f"single-instance lock unsupported: {error!r}", file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)

    return InstanceLock(fd, path)


def other_launcher_pids(script_name: str, *, self_pid: int | None = None,
                        procs=None) -> list[int]:
    """還活著、正在跑 `script_name` 這支啟動器的**其他**行程 pid。

    只給「已經有另一個實例在執行」那句訊息當診斷用——**永遠不參與決策**（決策是
    單一實例鎖的事），所以 psutil 缺席或不高興時回空 list 就好，不該擋住啟動。

    2026-09-03 從 `start_discord_bot.py` 搬上來給兩支啟動器共用。搬的時候把原本
    寫死的 `Path(__file__).name` 變成參數——放在這裡的話 `__file__` 會解析成
    `_supervisor.py`，永遠掃不到任何啟動器。

    `procs` 可注入（`(pid, name, cmdline, ppid)` 的可迭代物），所以這段判定測得
    起來而不必真的去開兩個啟動器。
    """
    from axiomatic._process_control import (  # 延後匯入：避免啟動期的相依環
        cmdline_runs_script,
        collapse_interpreter_stub_pairs,
        looks_like_python_process,
    )

    me = os.getpid() if self_pid is None else self_pid
    raw: list[tuple[int, int, str]] = []
    if procs is None:
        try:
            import psutil  # type: ignore
        except ImportError:
            return []

        def _iter():
            # 先用便宜的 `name` 過濾，再只對 Python 行程讀 `cmdline()`／`ppid()`。
            # psutil 在 Windows 上每取一次 ppid 就重建整台機器的對照表，寫成
            # `attrs=[..., "ppid"]` 是 O(N²)（實測 3.7 秒，而這是每次啟動都要跑
            # 的路徑）。
            for proc in psutil.process_iter(attrs=["pid", "name"]):
                try:
                    yield (proc.info.get("pid"), proc.info.get("name"),
                           proc.cmdline(), proc.ppid() or 0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        procs = _iter()

    try:
        for pid, name, cmdline, ppid in procs:
            if not looks_like_python_process(name):
                continue
            # 判定條件收緊成「參數就是這支腳本的路徑」：子字串比對會把任何
            # **提到**檔名的命令列（shell、`python -c`）算成「另一個實例」。
            try:
                hit = cmdline_runs_script(cmdline, (script_name,))
            except Exception:  # pylint: disable=broad-except
                continue
            if hit:
                # 自己這一筆**要留下**，交給 collapse 排除——它得看得到自己的
                # ppid，才排得掉「自己的轉接殼」那一半。
                raw.append((pid, ppid, "launcher"))
    except Exception:  # pylint: disable=broad-except
        pass
    # 啟動器經由 `.venv\Scripts\python.exe` 執行時，掃描會撞到「轉接殼 ＋ 本尊」
    # 兩筆（cmdline 一模一樣、父子關係）。不併的話，一個既有實例會被報成兩個
    # pid，讀的人以為自己真的開了兩份。
    return [pid for pid, _script in
            collapse_interpreter_stub_pairs(raw, exclude_pid=me)]


# ---- 子行程輸出的落地（兩支啟動器共用）------------------------------------
#
# 2026-08-23 這件事已經在 webrunner 那一側踩過一次並修好：啟動器原本是
# `subprocess.run(cmd)`，子行程直接繼承主控台，**關掉視窗那行就永遠找不回來**。
# 當時的修法只套用在 `start_webrunner.py`，`start_discord_bot.py` 原封不動地留著
# 同一個寫法到 2026-08-30。
#
# bot 那一側其實更嚴重，因為整條 Secrecy Layer 1 的設計就建立在「泛用訊息送
# Discord、完整細節寫 log」上——bot 甚至會回「請查看 log」。沒有落地的檔案時，
# 那句話指向的是一個不存在的東西，而所有 `print(..., file=sys.stderr)` 的診斷
# （包含背景 task 崩潰、原始例外文字、supervisor 放棄原因）都只活在某個沒人看的
# 主控台裡。
#
# 所以實作收在這裡一份，兩支啟動器都用它。**不要**再在啟動器裡各寫一次。

_LOG_PUMP_JOIN_SEC = 5.0


def trim_log(path, *, max_bytes: int, keep_bytes: int) -> None:
    """`path` 超過 `max_bytes` 時只保留最後 `keep_bytes`（切在整行邊界）。

    Best-effort：任何 I/O 失敗都只是不修剪，絕不讓監督者因為記錄檔而死。
    尾段保留而不是整個清空——會炸掉的正是「崩潰 → 重生」那條接縫，清掉就等於把
    要查的東西丟了。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    try:
        with path.open("rb") as handle:
            handle.seek(size - keep_bytes)
            handle.readline()          # 丟掉 seek 落點那半行
            tail = handle.read()
        path.write_bytes(tail)
    except OSError as error:
        print(f"trim_log({path.name}) failed: {error!r}", file=sys.stderr)


def echo_line(line: str) -> None:
    """把子行程的一行寫回啟動器自己的主控台。**任何情況下都不得往外丟例外。**

    子行程的輸出被強制成 UTF-8（見 `stream_child`），但**啟動器**的 stdout 不一定
    是主控台——被重導向到檔案時它會用系統地區編碼，遇到編不出來的字就
    `UnicodeEncodeError`。監督者不能因為一行日誌就掛掉，所以退成可替換寫法。

    這裡的兩層 `try` 是**分開的**，不是同一個 `try` 的兩個 `except`（2026-09-07
    修）。兩件事各自出過問題：

    1. 從 `except` 區塊裡丟出來的例外**不會**被同一個 `try` 的其他 `except`
       接住。退版寫法原本是 `except UnicodeEncodeError:` 裡直接寫 stdout、下面
       再掛一個 `except OSError: pass`——後者完全蓋不到前者。實測：第一次寫丟
       `UnicodeEncodeError`、退版那次丟 `OSError(28)`，那個 `OSError` 直接穿出
       本函式。
    2. **寫進已關閉的串流丟的是 `ValueError` 不是 `OSError`。** 本函式跑在抽水
       執行緒上，而 `pump_stream` 的 `except (OSError, ValueError)` 包的是整個
       迴圈——所以主控台壞掉一次就等於**整條抽水停掉**，子行程接著把管線塞滿
       然後永遠卡住。實測 3 行只抽到 1 行。那比沒有記錄檔更糟：監督者還活著、
       批次卻不動了，而且什麼都不會說。
    """
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass                    # 往下走可替換寫法
    except (OSError, ValueError):
        return
    # 退版路徑自己再包一層。`getattr` 取 `encoding`：這個物件不是我們控制的
    # （可能是任何重導向包裝），少一個屬性也不該讓監督者死在一行日誌上。
    # `LookupError`／`UnicodeError` 蓋的是「串流謊報自己的編碼」。
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeError, LookupError):
        pass


def log_write(log, line: str) -> None:
    """把一行寫進記錄檔，前面加時間戳（主控台那份維持原樣，不加前綴）。

    時間戳是事後對帳用的——`events.ndjson` 的 `ts` 要能跟這裡的行對得起來。
    """
    if log is None:
        return
    try:
        log.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}")
    except (OSError, ValueError):
        pass


def say(log, message: str, *, err: bool = False) -> None:
    """監督者自己的訊息：主控台 ＋ 記錄檔各一份。

    放棄原因、rc、退避秒數這些正是事後診斷要看的東西，只印在主控台等於沒留。
    """
    print(message, file=sys.stderr if err else sys.stdout)
    log_write(log, message.strip() + "\n")


def pump_stream(stream, log) -> None:
    """把子行程的輸出一行一行送到主控台 ＋ 記錄檔，讀到 EOF 為止。

    **這條抽水迴圈跑在自己的執行緒上，而且絕不能停**：只要給了 `stdout=PIPE`
    卻沒人讀，作業系統的管線緩衝區（Windows 上約 64 KB）一滿，子行程的下一個
    print 就永遠卡住——比原本沒有記錄檔更糟。獨立執行緒的用意是連 Ctrl+C 之後
    的收尾輸出也照抽，不會在等子行程收工時反而把它卡死。永不 raise。
    """
    if stream is None:
        return
    try:
        for line in stream:
            echo_line(line)
            log_write(log, line)
    except (OSError, ValueError):
        pass


def reap_child(proc, log, *, grace_sec: float, kill_sec: float) -> int:
    """Ctrl+C 之後把子行程收乾淨：先等它自己收工，逾時 terminate、再逾時 kill。

    為什麼不能直接把 KeyboardInterrupt 往上拋就走人：那會留下孤兒子行程（以及
    webrunner 那一側的整棵 Chrome）。`subprocess.run` 在 KeyboardInterrupt 時是
    `process.kill()`，Windows 上等於 TerminateProcess，子行程的 `finally` 完全
    不會跑。這裡先禮後兵。
    """
    try:
        return proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        say(log, "supervisor: child did not exit in time; terminating", err=True)
        proc.terminate()
    try:
        return proc.wait(timeout=kill_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def stream_child(cmd: list[str], log, *, cwd: str, on_spawn=None,
                 pump_name: str = "log-pump",
                 grace_sec: float = 30.0, kill_sec: float = 10.0) -> int:
    """跑一次子行程，把它的 stdout＋stderr 同時寫到主控台與記錄檔，回 rc。

    子行程強制 `PYTHONIOENCODING=utf-8`：管線不是主控台，CPython 會退回系統地區
    編碼（本機是 cp950），中文輸出就可能讓**子行程**自己炸掉。解碼端一併用
    `errors="replace"`，任何怪位元組都不會中斷監督。呼叫端給的 `-u` 也還是要留，
    否則子行程的輸出會在它自己的緩衝區裡積著、記錄檔變成一陣一陣的。

    **「強制」＝覆寫，不是 `setdefault`**（2026-09-12 修）。這一行原本是
    `env.setdefault(...)`，而上面這段說明從第一天就寫著「強制」——兩者差在呼叫端
    環境**已經帶著一個值**的時候誰贏。選覆寫的理由是下面 `Popen` 的解碼端是
    **寫死的** `encoding="utf-8"`：兩端只要不一致就是安靜的資料損壞，
    `errors="replace"` 保證不會有例外，於是 rc 正常、沒有紅字，只有記錄檔裡的繁中
    進度行、resume 不符的原因、放棄理由整段變成 U+FFFD。而啟動器最常見的起法
    （桌面捷徑、開機自動啟動、排程工作、別人的殼）正是「環境裡有一個我們沒設過的
    值」。本專案其餘 16 個交代子行程編碼的地方全部是覆寫
    （`{**os.environ, "PYTHONIOENCODING": "utf-8"}`），這裡曾是唯一的例外，方向還
    剛好是安靜壞掉的那一邊。

    也刻意**不**走「只有不是 UTF-8 變體才覆寫」的中間路線（放行
    `utf-8:surrogateescape` 之類）：那要多一段 codec 正規化（而 `codecs.lookup`
    自己會丟 `LookupError`），換來的只是保留呼叫端的錯誤處理器——那個處理器對我們
    這一端毫無作用，因為我們本來就 `errors="replace"`。監督者的記錄檔長什麼樣，
    不該取決於誰、從哪個殼把它點起來。

    行為測試兩半都有（`test_supervisor.py`）：環境裡沒有那個變數
    （`test_the_child_gets_utf8_io_encoding`），以及環境裡帶著一個錯的值
    （`test_a_wrong_pythonioencoding_in_the_parent_is_overridden`）。**只有後者
    分得出覆寫與 `setdefault`。** 這一站 `test_text_encoding` 的靜態掃描結構上
    看不到——`cmd` 是呼叫端給的變數，掃描器認不出這是 Python 子行程——所以這兩支
    具名測試就是它的全部覆蓋。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(  # nosec B603 — cmd 由呼叫端組出來，不經 shell
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        if on_spawn is not None:
            on_spawn(proc)
        pump = threading.Thread(target=pump_stream, args=(proc.stdout, log),
                                name=pump_name, daemon=True)
        pump.start()
    except Exception:  # pylint: disable=broad-except
        # 子行程**已經起來了**，所以這條路不能只是往上拋（2026-09-07 修）。
        # `on_spawn` 會做真的 I/O——`start_webrunner._on_spawn` 寫 `webrunner.pid`
        # ——磁碟滿了或權限不對就丟 OSError。拋掉的話會留下一個 stdout 是 PIPE、
        # 沒有人抽、也沒有人 wait 的孤兒：它下一次 print 就卡死在滿掉的管線裡，
        # 而它手上握著整棵 Chrome。監督者自己死掉、批次還在那裡卡著不動，正是
        # 最難查的那種收場。先收屍再把例外往上送。
        #
        # `grace_sec=0`：這裡的子行程**沒有**收到 Ctrl+C，沒有理由自己收工，等
        # 寬限期只是白等——直接 terminate，剩下的 Chrome 由 webrunner 下次啟動
        # 的 `_kill_orphan_chrome()` 掃掉。
        #
        # **不要**把這兩個 handler 改成 `except BaseException`：專案規則禁止
        # （`test_exception_handlers.test_nothing_swallows_cancellation`，理由是
        # 那會連 `CancelledError`／`KeyboardInterrupt` 一起吞掉），而這裡也不需要
        # ——真正會留下孤兒的是 `on_spawn` 丟 `OSError`（pid 檔寫不進去），那是
        # `Exception`。Ctrl+C 落在這個微小視窗裡的情況本來就由**主控台群組**兜著：
        # 子行程跟啟動器在同一個群組，同一個 Ctrl+C 它自己也收到了。
        try:
            say(log, "supervisor: spawn hook failed; terminating the child "
                     "that was already started", err=True)
            reap_child(proc, log, grace_sec=0, kill_sec=kill_sec)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass               # 收屍失敗也不能蓋掉原本那個例外
        raise
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # 同一個主控台群組，子行程也收到了 Ctrl+C。抽水執行緒還活著，所以它在
        # 收尾時大量輸出也不會把自己卡在滿掉的管線裡。
        reap_child(proc, log, grace_sec=grace_sec, kill_sec=kill_sec)
        raise
    finally:
        pump.join(timeout=_LOG_PUMP_JOIN_SEC)
        # 抽水還卡在 read 就**不要**關（2026-09-07 加的條件）。實測：
        # `BufferedReader.close()` 不會丟例外，也不會把串流從抽水手上抽走——它去
        # 搶同一把鎖，於是**一路擋到那次 read 回來為止**（量到 19.05 秒，正好是
        # 子行程還活著的時間）。
        #
        # 正常路徑走不到：`proc.wait()` 回來＝子行程已死＝管線 EOF＝抽水立刻結束。
        # 走得到的是「收屍失敗、子行程還活著」那條（`reap_child` 裡的
        # `terminate()`／`kill()` 自己丟例外）——那時候這一行會把監督者**永久**卡在
        # 收工的最後一步，而它手上還握著單一實例鎖，於是誰也重啟不了，畫面上什麼
        # 都沒有。抽水是 daemon 執行緒，行程結束時 OS 會把 fd 收掉。
        if proc.stdout is not None and not pump.is_alive():
            try:
                proc.stdout.close()
            except OSError:
                pass
