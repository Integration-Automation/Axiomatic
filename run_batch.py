"""PyCharm 一鍵批次啟動器。

依 todo_prompt.md / todo_character1.md / todo_character2.md /
todo_undesired.md 佇列直接開跑批次產圖，等同 bot 的 `!run`，但走本機
console：

1. 前置檢查：pause 標記殘留、已在跑的 instance、佇列全空 → 直接中止
2. 印出 run-plan 快照（與 webrunner 開跑時的 preflight 相同格式）
3. 交給 start_webrunner.py 的 supervised 迴圈實際 spawn（crash 自動
   退避重啟、todo 跑完 rc=0 乾淨結束、Ctrl+C 停止）

用法（PyCharm Run configuration 或命令列皆可）：
    py -3 run_batch.py                # selenium 變體（預設）
    py -3 run_batch.py je             # je 變體
    py -3 run_batch.py --clear-pause  # 清掉殘留的 pause 標記再跑

注意：不要跟 bot 的 `!run` 同時使用 — 兩邊會搶同一個
.chrome_profile/。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "axiomatic"))

# 允許的 passive shared modules（見 CLAUDE.md module boundaries）：
# _webrunner_shared 供 pause 標記路徑與 run_preflight 佇列快照，
# _process_control 供跨平台的 PID 存活探測。
from _process_control import _pid_alive  # noqa: E402
from _webrunner_shared import (  # noqa: E402
    WEBRUNNER_PAUSE_FILE,
    run_preflight,
)

PID_FILE = REPO_ROOT / "webrunner.pid"


def _bot_spawned_pid() -> int | None:
    """讀 bot 寫的 webrunner.pid；沒有或壞掉回 None。

    這裡回 `None` 走的是樂觀的那一邊（＝「往下跑」），和
    `start_webrunner._live_webrunner_pid` 2026-09-07 修掉的那個缺陷是同一個形狀
    ——但**在這支是安全的**，因為它不是把關的那一層：這支不 spawn webrunner，
    它印完佇列快照就把工作交給 `start_webrunner.py` 子行程，而那支在持有 Chrome
    槽的狀態下會再判一次，判不出來就拒絕啟動並回 rc=1（本函式的呼叫端原封不動
    把那個 rc 往上送）。所以這裡讀不出 pid 檔的下場只是「多印一份 run-plan
    快照」——`run_preflight` 是唯讀的——然後被下游擋下來。
    `ValueError` 也**連帶**接住了 `UnicodeDecodeError`（它是 `ValueError` 的
    子類），所以不會像修之前的啟動器那樣直接炸穿。

    要動這裡之前先確認上面那句仍然成立：如果哪天 `run_batch.py` 改成自己直接
    spawn webrunner，這個 `None` 就會變成真正的缺陷，那時要照啟動器的做法回
    `(pid, 判定得出來嗎)`。
    """
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="直接依 todo 佇列開跑批次產圖（supervised）。"
    )
    parser.add_argument(
        "variant",
        nargs="?",
        default="selenium",
        choices=["je", "selenium"],
        help="webrunner 變體（預設 selenium）",
    )
    parser.add_argument(
        "--clear-pause",
        action="store_true",
        help="開跑前刪除殘留的 webrunner.pause 標記",
    )
    args = parser.parse_args()

    # 1) 已有 instance 在跑（多半是 bot 的 !run 啟的）就不搶 Chrome profile。
    #
    #    **這一步必須在 pause 處理之前。** 反過來的話，
    #    `run_batch.py --clear-pause` 會先把標記刪掉、才發現有 instance 在跑然後
    #    中止——而那個「已經刪掉」是留在磁碟上的：正在跑而且**被暫停中**的正式
    #    批次會立刻恢復產圖，使用者卻只看到「先打 !stop」以為什麼都沒發生。
    #    中止的指令不可以留下持久的副作用。
    pid = _bot_spawned_pid()
    if pid and _pid_alive(pid):
        print(
            f"已有 webrunner instance 在跑（PID {pid}）。\n"
            "先在 bot 打 !stop（或結束該行程）再跑。",
            file=sys.stderr,
        )
        return 1

    # 2) pause 標記：webrunner 所有 run（含手動）都會在安全邊界看這個
    #    檔案並停住等待；殘留時直接開跑會看起來像卡死，所以先擋下來。
    if WEBRUNNER_PAUSE_FILE.exists():
        if args.clear_pause:
            WEBRUNNER_PAUSE_FILE.unlink(missing_ok=True)
            print("已清除殘留的 pause 標記")
        else:
            print(
                "webrunner.pause 標記存在：現在開跑會停在安全邊界等待。\n"
                "先在 bot 打 !resume，或加 --clear-pause 重跑。",
                file=sys.stderr,
            )
            return 1

    # 3) 佇列快照 + 全空中止（與 webrunner 自己的 preflight 同一個函式）。
    if not run_preflight(oneshot_pending=False):
        return 1

    # 4) 交給 supervised launcher 實際 spawn；它自己會挑對的直譯器
    #    （.venv → py -3 → sys.executable）跑 webrunner。
    cmd = [sys.executable, str(REPO_ROOT / "start_webrunner.py"), args.variant]
    try:
        return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode
    except KeyboardInterrupt:
        print("\nrun_batch: Ctrl+C — 停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
