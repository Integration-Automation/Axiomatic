"""直接叫醒工作排程器裡的監督者（`\\Axiomatic\\` 底下那些工作）。

用法（repo 根目錄）::

    py -3 wake_autostart.py                  # 全部叫醒（已經在跑的略過）
    py -3 wake_autostart.py --bot-only       # 只叫醒平台的監督者（每個平台一支）
    py -3 wake_autostart.py --batch-only     # 只叫醒批次的監督者（會開始產圖）

**叫醒的是排程器裡現在真的有的工作**，不是設定檔算出來的那一份。bot 的工作是
**一個平台一筆**（`\\Axiomatic\\Bot-<平台>`），而剛被關掉的平台，它的工作仍然留在
排程器裡——用「現在開著哪些」去算要叫醒誰，那一筆就會變成永遠叫不醒也刪不掉。

工作本身由 `install_autostart.py --install` 註冊；這支只負責「現在就跑一次」，
跟登入時自動觸發的是同一份工作、同一個直譯器、同一個工作目錄。

**為什麼不直接執行 `start_discord_bot.py`。** 從某個終端機或 IDE 直接啟動時，監督者
和它底下的 bot、webrunner 都會繼承那個終端機的環境變數（`PYCHARM_HOSTED`、IDE 塞進來
的 `PYTHONPATH`、開發工具自己的工作階段變數……），bot 再原樣傳給後端 CLI 子行程。
透過排程器啟動，拿到的是登入工作的乾淨環境，和重新開機後自動拉起來的那一份完全一樣。

**`/sys restart` 需要監督者。** 它只是結束 bot、等監督者重生；bot 若是直接啟動、
沒有監督者，就只會關掉（2026-09-22 實際發生）。那時用這支把監督者叫起來。

**預設兩支都叫醒**（擁有者 2026-09-22 裁定），和登入時自動啟動的那一組一樣。批次一起來
就開始消耗佇列產圖，所以刻意用 `/stop` 停掉批次、只想把 bot 拉回來時，要用 `--bot-only`。

**不會多開。** 已經有啟動器在跑的那一支直接略過（`_supervisor.other_launcher_pids`，
只是診斷用）；就算這個判斷失準，排程器的 `IgnoreNew` 與啟動器自己的單一實例鎖也會
擋掉第二份。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from install_autostart import (BATCH_TASK, _schtasks,
                               _wanted_task_names)
from axiomatic._supervisor import other_launcher_pids

# 叫醒之後等啟動器出現在行程表的上限與輪詢間隔（秒）。排程器接受 `/Run` 之後，
# 啟動器通常一兩秒內就看得到；等不到不算失敗（可能只是慢，或被單一實例鎖擋掉），
# 改成叫人去看 `install_autostart.py --status` 的「上次結果」。
APPEAR_TIMEOUT_SEC = 15.0
APPEAR_POLL_SEC = 0.5


def _pid_text(pids: list[int]) -> str:
    return "、".join(str(pid) for pid in pids)


def task_script(task_name: str) -> tuple[str, str | None]:
    """工作名 →（那筆工作跑的腳本, 用來分辨平台的那個參數）。

    bot 那幾筆共用同一個腳本檔名，所以「它在跑嗎」不能只比檔名——否則其中一個平台
    的那一支會替別的平台回答「在跑」。平台名從工作名的後綴取回來。
    """
    if task_name == BATCH_TASK[0]:
        return BATCH_TASK[1], None
    return "start_discord_bot.py", task_name.rsplit("-", 1)[-1]


def wake(task_name: str, *, run=_schtasks, running=other_launcher_pids,
         sleep=time.sleep, clock=time.monotonic,
         timeout: float = APPEAR_TIMEOUT_SEC) -> int:
    """叫醒 `task_name` 那一份工作；回傳 0＝已經在跑或已叫醒，1＝沒註冊或排程器拒絕。

    `run`（排程器呼叫）、`running`（找啟動器行程）、`sleep`、`clock` 都可以注入，
    測試因此不必碰真的工作排程器與行程表。排程器只會被要求執行**一次**：等不到
    啟動器出現時不重試，因為第二次 `/Run` 在 `IgnoreNew` 之下什麼都不會做，只會讓
    訊息變得難懂。
    """
    script, platform = task_script(task_name)
    label = script if platform is None else f"{script} --platform {platform}"
    already = running(script, also_contains=platform)
    if already:
        print(f"{task_name}：{label} 已經在跑（pid {_pid_text(already)}），不再叫醒。")
        return 0
    if run("/Query", "/TN", task_name).returncode != 0:
        print(f"{task_name}：這份工作還沒註冊。先執行 "
              "py -3 install_autostart.py --install", file=sys.stderr)
        return 1
    done = run("/Run", "/TN", task_name)
    if done.returncode != 0:
        print(f"{task_name}：排程器拒絕執行：{done.stdout.strip()} "
              f"{done.stderr.strip()}", file=sys.stderr)
        return 1
    deadline = clock() + timeout
    while clock() < deadline:
        pids = running(script, also_contains=platform)
        if pids:
            print(f"{task_name}：已叫醒，{label} 在跑（pid {_pid_text(pids)}）。")
            return 0
        sleep(APPEAR_POLL_SEC)
    print(f"{task_name}：排程器已經接受，但 {timeout:.0f} 秒內沒看到 {label}。"
          "用 py -3 install_autostart.py --status 看「上次結果」。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="直接叫醒工作排程器裡的監督者（預設全部叫醒）。")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--bot-only", action="store_true",
                       help="只叫醒平台的監督者（每個平台一支）")
    scope.add_argument("--batch-only", action="store_true",
                       help="只叫醒批次的監督者（會開始產圖）")
    args = parser.parse_args(argv or [])

    if os.name != "nt":
        print("這支腳本只支援 Windows 工作排程器。", file=sys.stderr)
        return 1

    which = (["bot"] if args.bot_only
             else ["batch"] if args.batch_only
             else ["bot", "batch"])
    targets = _wanted_task_names(which)
    if not targets:
        print("`\\Axiomatic\\` 底下沒有符合的工作。先執行 "
              "py -3 install_autostart.py --install", file=sys.stderr)
        return 1
    rc = 0
    for task_name in targets:
        rc |= wake(task_name)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
