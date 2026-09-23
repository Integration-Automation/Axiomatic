"""行程層級的電源要求（Windows）——參考計數，其他平台是 no-op。

被動共用模組：純 stdlib（`ctypes`），不 import 任何專案模組，也不碰瀏覽器或
聊天平台。bot 在批次監督期間、以及 Dorossi 工作進行中經 `discord_bot.bot_power_hold` 持有它。批次的
`_webrunner_shared.StayAwake` 用的是同一組要求型別，改成委派給這裡、讓整個專案
只剩**一份**實作，是另一組變更——那個模組在正式批次的匯入閉包裡，只能在沒有批次
在跑的時候套用。

## 它保證什麼、不保證什麼

要求型別與批次原本的 `StayAwake` 完全相同：`PowerCreateRequest` ＋
`PowerSetRequest(PowerRequestExecutionRequired)`，拿不到才退回
`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`。

* `PowerRequestExecutionRequired` 保的是**呼叫它的這個行程**：Modern Standby
  （S0ix）期間不被 PLM／桌面活動調節器暫停。它**不會**讓系統不進入待命——本機
  2026-09-20 實測過，持有要求時系統照樣進入 Modern Standby，而持有的行程照樣
  在跑（完整證據在 `_webrunner_shared` 的 `StayAwake` 區塊註解）。只有傳統 S3
  機器上它才順帶隱含 `PowerRequestSystemRequired`。
* **只保本行程，不保子行程。** 微軟文件的措辭是「the calling process continues
  to run」。bot 持有要求並不會讓它 spawn 出去的子行程（批次、後端 CLI）一起豁免；
  批次自己另外持有一份（`StayAwake`），後端 CLI 子行程目前沒有。
* **※ 用電池跑的時候會被系統撤銷**：Modern Standby 機器在 DC 電源下，`system`
  與 `execution required` 兩種要求會在「系統睡眠逾時過後 5 分鐘」被終止。程式端
  無解，無人值守就插著電。
* `SetThreadExecutionState` 備援只擋得住舊的 S3 閒置睡眠，而且它是**每條執行緒**
  各自的狀態：誰設的就只有那條執行緒清得掉。實務上它只會在電源要求物件拿不到的
  舊機器上出場。
* 刻意**不**要求螢幕保持開啟（沒有 display 那一型）。

## 參考計數

同一個行程裡可能同時有好幾個理由要保持清醒（bot：批次監督、Dorossi
的回合與自走迴圈）。第一個 `acquire` 才真的向作業系統要，最後一個 `release` 才真的放。
每一次 `acquire` 回一個 `PowerHold`，它的 `release()` 是冪等的——同一個 hold
放兩次只算一次，所以 `finally` 裡多叫一次不會把別人的份一起放掉。

    hold = acquire("batch supervision")
    try:
        ...
    finally:
        hold.release()

    with hold("dorossi turn"):          # 等價寫法；例外與取消都會放掉
        ...

對 asyncio 是安全的：每個呼叫都是同步、微秒級、不 await，所以可以直接在事件
迴圈上呼叫；`CancelledError` 穿過 `with` 時 `__exit__` 照樣會跑。對執行緒也是
安全的：計數與作業系統呼叫都在同一把鎖裡。

**永不 raise**：這是加分項。拿不到就回 `active=None` 往下跑——為了一個省電設定
讓監督或批次起不來，方向是反的。

**被回收不會自動放掉**（刻意沒有 `__del__`）：漏放的後果是「這個行程到結束前都
不會被暫停」，而行程一結束作業系統就收回要求，不會留下永遠生效的要求。
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading

# `active` 的兩種字面值。批次的 log 會把它原樣印出來（見 `_webrunner_shared`
# `run_batch` 那三行），事後 grep log 才問得出「那一輪到底拿到了哪一種」。
POWER_REQUEST = "power-request"
EXECUTION_STATE = "execution-state"

_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_EXECUTION_REQUIRED = 3      # 保的是「本行程不被 PLM 暫停」
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

DEFAULT_REASON = "axiomatic is working"


def _load_kernel32():
    """測試的接縫：換掉它就能在不碰真實電源狀態的情況下走完整條 Windows 路徑。"""
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


class _WindowsBackend:
    """真的向作業系統要／放。一次只持有一份（參考計數在 `_Manager`）。"""

    def __init__(self) -> None:
        self.active: str | None = None
        self._handle = None
        self._kernel32 = None

    def acquire(self, reason: str) -> str | None:
        """回 `POWER_REQUEST` / `EXECUTION_STATE` / None。永不 raise。

        **`argtypes`／`restype` 一定要寫**，理由與 `CLAUDE.md` 那條 PID 存活探測
        相同：`PowerCreateRequest` 回的是 64 位元 HANDLE，預設 `c_int` 會把它截斷，
        而截斷後的 handle 仍然非零，`if not handle` 抓不到。

        平台判斷放在**這裡**、每次取得時才問，不在建立後端時問：測試把 `os.name`
        換掉就能走到非 Windows 那條路，而不必重建整個計數器。
        """
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:  # pylint: disable=broad-except
            return None
        try:
            kernel32 = _load_kernel32()
            self._kernel32 = kernel32

            class _Context(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG),
                            ("Flags", wintypes.ULONG),
                            ("SimpleReasonString", wintypes.LPWSTR)]

            kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_Context)]
            kernel32.PowerCreateRequest.restype = wintypes.HANDLE
            kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
            kernel32.PowerSetRequest.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            context = _Context(_POWER_REQUEST_CONTEXT_VERSION,
                               _POWER_REQUEST_CONTEXT_SIMPLE_STRING, reason)
            handle = kernel32.PowerCreateRequest(ctypes.byref(context))
            # INVALID_HANDLE_VALUE 是 -1，不是 0——只檢查 falsy 會漏掉它。
            if handle and handle != wintypes.HANDLE(-1).value:
                if kernel32.PowerSetRequest(
                        handle, _POWER_REQUEST_EXECUTION_REQUIRED):
                    self._handle = handle
                    self.active = POWER_REQUEST
                    return self.active
                kernel32.CloseHandle(handle)
        except Exception as error:  # pylint: disable=broad-except
            # `!r` 刻意保留：`try` 裡只有 ctypes／WinDLL，碰不到瀏覽器驅動。
            # 字句沿用批次 `StayAwake` 原本那一行，批次的 log 讀起來不變。
            print(f"  [power] 電源要求拿不到（{error!r}）；改用舊 API",
                  file=sys.stderr)

        # 備援：還有 S3 的機器靠這個就夠；S0ix 機器上它既擋不住待命、也保不住
        # 行程不被 PLM 暫停，但拿著也沒有壞處。
        try:
            kernel32 = _load_kernel32()
            kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
            kernel32.SetThreadExecutionState.restype = wintypes.DWORD
            if kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
                self._kernel32 = kernel32
                self.active = EXECUTION_STATE
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        return self.active

    def release(self) -> None:
        """冪等、永不 raise。"""
        kernel32, handle, active = self._kernel32, self._handle, self.active
        self._handle, self.active = None, None
        if kernel32 is None:
            return
        try:
            from ctypes import wintypes
            if handle is not None:
                kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE,
                                                       wintypes.INT]
                kernel32.PowerClearRequest.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.PowerClearRequest(handle,
                                           _POWER_REQUEST_EXECUTION_REQUIRED)
                kernel32.CloseHandle(handle)
            elif active == EXECUTION_STATE:
                kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass


def _default_backend_factory():
    """預設後端。非 Windows 上它的 `acquire` 直接回 None（＝ no-op）。"""
    return _WindowsBackend()


class PowerHold:
    """一次 `acquire` 的收據。`release()` 冪等；也可以當 context manager 用。"""

    def __init__(self, manager: "_Manager | None", reason: str,
                 active: str | None) -> None:
        self._manager = manager
        self.reason = reason
        # 取得當下整個行程實際持有的是哪一種（`None`＝沒拿到或非 Windows）。
        self.active = active
        self.released = manager is None

    def release(self) -> None:
        # 冪等是結構上的：第一次就把 `_manager` 拿走，之後再叫什麼都碰不到。
        manager, self._manager = self._manager, None
        self.released = True
        if manager is not None:
            manager._release(self)

    def __enter__(self) -> "PowerHold":
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:
        state = "released" if self.released else "held"
        return f"PowerHold({self.reason!r}, active={self.active!r}, {state})"


class _Manager:
    """行程內唯一的計數器。測試可以用自己的後端另建一個。"""

    def __init__(self, backend_factory=None) -> None:
        self._lock = threading.RLock()
        self._factory = backend_factory or _default_backend_factory
        self._backend = None
        self._holds: list[PowerHold] = []
        self.active: str | None = None

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._holds)

    def reasons(self) -> list[str]:
        with self._lock:
            return [hold.reason for hold in self._holds]

    def acquire(self, reason: str = DEFAULT_REASON) -> PowerHold:
        reason = str(reason or DEFAULT_REASON)
        with self._lock:
            # 還沒拿到（第一個、或上一次向系統要失敗了）就再要一次；已經拿到
            # 就只加計數。失敗不 raise，hold 照樣記帳——放的時候才對得上。
            if self.active is None:
                try:
                    if self._backend is None:
                        self._backend = self._factory()
                    if self._backend is not None:
                        self.active = self._backend.acquire(reason)
                except Exception:  # pylint: disable=broad-except
                    self.active = None
            hold = PowerHold(self, reason, self.active)
            self._holds.append(hold)
            return hold

    def _release(self, hold: PowerHold) -> None:
        with self._lock:
            # 用身分比對，不用 `==`：兩個同理由的 hold 是兩份。
            for index, item in enumerate(self._holds):
                if item is hold:
                    del self._holds[index]
                    break
            else:
                return
            if self._holds:
                return
            backend, self.active = self._backend, None
            if backend is not None:
                try:
                    backend.release()
                except Exception:  # pylint: disable=broad-except  # nosec B110
                    pass


_MANAGER = _Manager()


def acquire(reason: str = DEFAULT_REASON) -> PowerHold:
    """取得一份（計數 +1）。回傳的 hold 用完要 `release()`。永不 raise。"""
    return _MANAGER.acquire(reason)


@contextlib.contextmanager
def hold(reason: str = DEFAULT_REASON, *, enabled: bool = True):
    """`with hold(...)`：進入時取得、離開時（含例外與取消）放掉。

    `enabled=False` 回一個不計數、什麼都不做的 hold——讓呼叫端用同一種寫法處理
    「設定關掉了」，不必自己分兩條路。
    """
    held = _MANAGER.acquire(reason) if enabled else PowerHold(None, reason, None)
    try:
        yield held
    finally:
        held.release()


def inert(reason: str = DEFAULT_REASON) -> PowerHold:
    """不計數、什麼都不做、`release()` 可呼叫的 hold（設定關掉時用）。"""
    return PowerHold(None, reason, None)


def status() -> dict:
    """診斷用：目前幾份、實際拿到哪一種、各自的理由。"""
    return {"count": _MANAGER.count, "active": _MANAGER.active,
            "reasons": _MANAGER.reasons()}
