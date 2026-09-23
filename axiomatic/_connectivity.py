"""「這台主機現在連得上網路嗎？」——給兩個批次監督者判斷失敗是不是斷網造成的。

被動共用模組：純 stdlib（`socket`），不 import 任何專案模組、不寫檔、不起行程。
bot 的監督者（`discord_bot._watch_for_fallback`）與獨立監督者
（`start_webrunner._supervise`）共用這一份判準。

## 為什麼需要它

兩個監督者都有「放棄閘」：連續幾次秒崩（rapid-fail）或連續幾輪零產出就停下來等
人。那兩道閘是為了**站方或本機壞掉**設計的——重生解決不了，所以停。但斷網也會
讓批次每一輪都秒崩或零產出，而斷網是**會自己好的**：斷四十分鐘，閘就響了，網路
回來之後沒有任何東西會把批次重新跑起來，無人值守的一整夜就這樣停在那裡。

所以失敗的當下多問一句「網路還在嗎」：**明確斷網**時那一次失敗不算進放棄閘，改成
等網路回來再接續（批次本身會從 `_run_progress` 的檢查點逐角色接回去）。

## 「明確斷網」的判準

對幾個公開、任播（anycast）的位址直接開 TCP 連線（不經過 DNS）。**任何一個連得上
就算有網路**；全部失敗才算斷網。刻意偏向「有網路」：誤判成有網路，只是回到原本的
行為（那一次失敗照常計數）；誤判成斷網，才會讓一個真正壞掉的批次一直等下去。
所以只有「一個都連不上」才算。

站方本身掛掉、而主機網路正常時，這裡回「有網路」——那正是放棄閘該管的情況，
這兩件事因此分得開。

探測對象不寫進任何送到聊天平台的字串（Secrecy Layer 1）；它們只是位址。
"""
from __future__ import annotations

import socket
import time

# 公開 DNS 服務的任播位址，連 443 埠。三家不同業者，避免單一業者故障被誤判成斷網。
# 用 IP 而不是主機名稱：斷網時 DNS 查詢本身可能要卡好幾秒才失敗，而「DNS 壞了但
# IP 通」並不是這裡要回答的問題。
PROBE_ADDRESSES: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 443),
    ("9.9.9.9", 443),
)
PROBE_TIMEOUT_SEC = 3.0
# 等網路回來時多久探一次。
DEFAULT_POLL_SEC = 30.0


def is_online(addresses=None, *, timeout: float = PROBE_TIMEOUT_SEC,
              connect=None) -> bool:
    """任何一個探測位址連得上就回 True。永不 raise。

    `connect` 是測試的接縫（預設 `socket.create_connection`）。空的位址清單回
    True——「沒有東西可以探」不能被讀成「斷網」，那會讓監督者無限等下去。
    """
    targets = PROBE_ADDRESSES if addresses is None else tuple(addresses)
    if not targets:
        return True
    opener = connect or socket.create_connection
    for host, port in targets:
        try:
            sock = opener((host, port), timeout)
        except OSError:
            continue
        except Exception:  # pylint: disable=broad-except
            # 不是網路錯誤（例如替身寫錯）→ 判不出來 → 偏向「有網路」。
            return True
        try:
            close = getattr(sock, "close", None)
            if close is not None:
                close()
        except OSError:
            pass
        return True
    return False


def wait_until_online(*, poll_sec: float = DEFAULT_POLL_SEC, probe=None,
                      sleep=None, on_wait=None) -> float:
    """同步地等到 `probe()` 回 True，回傳等了幾秒（單調時鐘）。

    給同步的呼叫端（獨立監督者）用；bot 在事件迴圈上有自己的 async 版本。**沒有
    次數或時間上限**——擁有者的裁定是只有明確的停止指令能結束等待，所以這裡能
    結束等待的只有 `probe()` 回 True，或 `sleep` 丟出例外（Ctrl+C 的
    `KeyboardInterrupt` 會原樣往外丟給呼叫端處理）。

    `on_wait(elapsed)` 在每次睡之前呼叫一次，讓呼叫端決定要不要記一行。
    """
    check = probe or is_online
    nap = sleep or time.sleep
    started = time.monotonic()
    while not check():
        if on_wait is not None:
            on_wait(time.monotonic() - started)
        nap(max(0.1, float(poll_sec)))
    return time.monotonic() - started
