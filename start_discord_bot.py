"""Launcher for **one** axiomatic chat-platform process.

One platform, one process. ``--platform <name>`` (default ``discord``) decides
which platform this supervisor serves; everything this process owns — its
single-instance lock, its log file and the bot's own state files — is named
after that platform and lives under ``state/<platform>/``. So a second
supervisor for a second platform is simply this script again with a different
``--platform``, and neither can block, overwrite or restart the other.
``start_platforms.py`` is the convenience wrapper that starts one of these per
enabled platform.

Runs ``axiomatic/discord_bot.py`` in a supervised loop: if the bot process
exits for any reason (network outage, unhandled exception, token reload, etc.)
this launcher waits a short backoff and restarts it. Ctrl+C in this launcher
window stops the loop cleanly.

discord.py itself handles short-lived gateway disconnects via its own
reconnect logic; this loop is the outer net for everything else.

The bot subprocess is started with the interpreter ``python_command()``
picks — the repo-local ``.venv`` first, then the Windows ``py -3`` launcher,
then the interpreter that started this launcher (DoD #5 in ``CLAUDE.md``) —
so it doesn't matter which Python you used to invoke this script. Under the
``.venv`` interpreter every program shows up as **two** processes with the
same command line: CPython's venv redirector and the real interpreter under
it. That is expected, not a duplicate launch (the launcher's own
§8.5).

Usage:
    py -3 start_discord_bot.py
    py -3 start_discord_bot.py --platform telegram
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from axiomatic import _platform_runtime
from axiomatic._supervisor import (
    acquire_single_instance_lock,
    child_exit_is_fatal,
    other_launcher_pids,
    restart_backoff,
    say,
    stream_child,
    trim_log,
)


REPO_ROOT = Path(__file__).resolve().parent
BOT_SCRIPT = REPO_ROOT / "axiomatic" / "discord_bot.py"

# 這支監督者服務哪一個平台。**在模組層決定一次**，底下的鎖檔與記錄檔都掛在它上面。
PLATFORM = _platform_runtime.active_platform()

# 單一實例鎖。擋的是「這支 launcher 被啟動兩次」——實際發生過：兩個 supervisor
# 加兩個 bot 同時在跑，各自跑 presence 迴圈、各自鏡像、各自消耗佇列，而且兩套
# 都「看起來正常」，從外面完全看不出來。鎖由 OS 持有，行程一消失就釋放。
#
# **逐平台一把。** 共用一把的話，第二個平台的監督者會被第一個平台的鎖擋掉，而
# 那個症狀（「啟動了卻說已經有另一個實例」）看起來像重複啟動，不像設計。
LOCK_FILE = _platform_runtime.platform_file(
    REPO_ROOT / ".discord_bot_supervisor.lock", platform=PLATFORM)

# 子行程的主控台輸出**同時**落地成檔案。
#
# 這件事 2026-08-23 已經在 webrunner 那一側踩過並修好（見 `start_webrunner.py`
# 的 `WEBRUNNER_LOG` 註解）：啟動器原本是 `subprocess.run(cmd)`，子行程直接繼承
# 主控台，關掉視窗那行就永遠找不回來。當時只改了那一支。
#
# bot 這一側其實更嚴重：整條 Secrecy Layer 1 的設計就是「泛用訊息送 Discord、
# 完整細節寫 log」，bot 甚至會回「請查看 log」。沒有這個檔的時候，那句話指向一個
# 不存在的東西，而所有 `print(..., file=sys.stderr)` 的診斷——背景 task 崩潰、
# 原始例外文字、supervisor 放棄原因——都只活在某個沒人看的主控台裡。
#
# 與 webrunner 那一側的差別：那個檔 bot 每次 spawn 會清空（一次 `/run` 一份），
# 這個檔只**附加**，成長由 `trim_log` 以尾段保留法封頂。會炸掉的正是「崩潰 →
# 重生」那條接縫，清掉就等於把要查的東西丟了。
# **逐平台一份。** 兩個平台寫同一份記錄檔的話，`trim_log` 的尾段保留會把另一個
# 平台的崩潰原因一起裁掉，而那正是這個檔存在的理由。
BOT_LOG = _platform_runtime.platform_file(
    REPO_ROOT / "discord_bot.log", platform=PLATFORM)
LOG_MAX_BYTES = 8 * 1024 * 1024     # 超過就修剪
LOG_KEEP_BYTES = 4 * 1024 * 1024    # 修剪後保留的尾段

# Backoff between restarts. The bot uses exponential backoff up to a cap so
# we don't hammer Discord if something is wrong (e.g. bad token).
RESTART_DELAY_MIN = 5    # seconds — first retry
RESTART_DELAY_MAX = 300  # seconds — cap (5 minutes)


def python_command() -> list[str]:
    """Prefer the repo-local `.venv` interpreter, then `py -3`, then the
    interpreter that started this launcher."""
    if os.name == "nt":
        venv_py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return [str(venv_py)]
    py = shutil.which("py")
    if py:
        return [py, "-3"]
    return [sys.executable]


def _other_launcher_pids() -> list[int]:
    """其他還活著、正在跑這支啟動器的行程 pid（診斷用，不參與決策）。

    實作已於 2026-09-03 上移到 `_supervisor.other_launcher_pids`，好讓
    `start_webrunner.py` 用同一份——兩支啟動器的「已經有另一個實例」訊息不該有
    兩種行為。這裡只留一層薄包裝，把這支腳本自己的檔名餵進去。
    """
    return other_launcher_pids(Path(__file__).name)


def main() -> int:
    if not BOT_SCRIPT.exists():
        print(f"missing {BOT_SCRIPT}", file=sys.stderr)
        return 1

    # 這個平台的狀態目錄。鎖檔與記錄檔都住在裡面，所以要在碰它們之前建好。
    # 放在 `main()` 而不是模組層：import 一個啟動器不該在版本庫裡建目錄。
    _platform_runtime.ensure_state_dir(PLATFORM)
    trim_log(BOT_LOG, max_bytes=LOG_MAX_BYTES, keep_bytes=LOG_KEEP_BYTES)
    try:
        log = BOT_LOG.open("a", encoding="utf-8", buffering=1)
    except OSError as error:
        # 落不了地就照舊只印主控台——監督者不能因為記錄檔而不啟動。
        print(f"supervisor: cannot open log ({error!r}); console only",
              file=sys.stderr)
        log = None

    try:
        # 鎖要綁在區域變數上並活到 return，但**理由不是「被回收就會放掉」**——
        # `InstanceLock` 刻意沒有 `__del__`（本機實測；
        # `test_supervisor.test_instance_lock_must_not_grow_a_del_method`
        # 在守），所以丟掉參照只是洩漏一個 fd，鎖仍然握著到行程結束。留著這個名字是
        # 為了讓「收尾」在程式碼上看得出來。**不要**因為讀到舊的那句說法就去補一個
        # `__del__`：補完之後才會真的長出它警告的那個缺陷——呼叫端只要沒把它存進變數，
        # 鎖就被靜默放掉，第二個監督者當場起得來。
        lock = acquire_single_instance_lock(LOCK_FILE)
        if lock is None:
            # pid 清單只是診斷，而且**跨平台**：同一支啟動器服務別的平台時也長
            # 這個名字，所以措辭不說「就是這幾個佔著」，只說「這幾個也在跑這支」。
            others = _other_launcher_pids()
            detail = (f"（同一支啟動器的其他行程 pid: {', '.join(map(str, others))}"
                      "，其中可能有別的平台的）") if others else ""
            say(log, f"supervisor[{PLATFORM}]: 這個平台已經有另一個實例在執行"
                     f"{detail}，這次不啟動。", err=True)
            say(log, f"supervisor[{PLATFORM}]: 要改跑這一個的話，先結束既有的那個"
                     "再重新啟動。別的平台不受影響，它們各有自己的鎖。", err=True)
            return 1
        if lock.degraded:
            # 降級不是錯誤，但**一定要講出來**：拿不到互斥保護卻照常啟動是刻意的
            # 取捨（見 `acquire_single_instance_lock`），而無聲的降級等於根本沒有
            # 這把鎖——重複實例會在幾天後以別的症狀出現，那時沒人回推得到這裡。
            # 兩支啟動器的措辭刻意一致。
            say(log, "supervisor: 注意——這台機器上的單一實例保護沒有生效，"
                     "這次不會擋掉重複啟動，請自己確認只開了一個。")

        try:
            # 平台名走 argv（`_platform_runtime.platform_from_argv`）。**不靠環境
            # 變數傳**：`stream_child` 自己組子行程的環境，而一個經由環境傳下去的
            # 身分只要有人在中間清掉環境就會安靜地變回預設平台——那個行程會開始寫
            # 另一個平台的狀態檔，而且沒有任何地方會講。
            cmd = python_command() + ["-u", str(BOT_SCRIPT),
                                      "--platform", PLATFORM]
            say(log, f"supervisor[{PLATFORM}] launching: {' '.join(cmd)}")
            delay = RESTART_DELAY_MIN
            attempt = 0
            while True:
                attempt += 1
                # **單調時鐘，不是牆鐘。** 這個值只用來量「bot 活了多久」，
                # 從不離開本行程、不寫檔、也不跟檔案 mtime 比對（判準見
                # `_chrome_slot.acquire` 的 docstring）。`time.time()` 會被 NTP 的
                # step 修正、手動改時鐘、虛擬機快照還原跳動（換時區與日光節約時間
                # **不會**，那個回的是 UTC epoch 秒），而它餵的正是下面那個
                # `healthy=` 判定：往回撥 → 活得好好的 bot 被當成剛崩潰、退避一路
                # 長上去；往前撥 → 看起來活很久、退避被重置成 5 秒，於是一個 token
                # 壞掉的 bot 會以近乎固定的 5 秒間隔一直重連——那正是這段退避設計
                # 要避免的「把連線端點打爆而被封鎖」。
                start = time.monotonic()
                try:
                    rc = stream_child(cmd, log, cwd=str(REPO_ROOT),
                                      pump_name="bot-log-pump")
                except KeyboardInterrupt:
                    say(log, "\nsupervisor: Ctrl+C — stopping")
                    return 0
                ran_for = time.monotonic() - start   # 單調時鐘，見上面 `start`
                # 這一種失敗重試永遠不會成功：bot 自己也有一把單一實例鎖，被擋掉時
                # 每次都在一秒內乾淨地退出。照常退避的話，這個迴圈會每 5～300 秒重
                # 生一次註定被同一把鎖擋掉的子行程、每輪印一行，而 rapid-fail giveup
                # 只看「跑多久」、看不出這件事。直接收工並說明原因。
                if child_exit_is_fatal(rc):
                    say(
                        log,
                        "supervisor: bot 回報已經有另一個實例在執行"
                        "（多半是有人直接執行 discord_bot.py，沒有經過啟動器），"
                        "重試不會有幫助；這個啟動器結束。",
                        err=True,
                    )
                    say(log, "supervisor: 先結束那一個，再重新啟動這支啟動器。",
                        err=True)
                    return rc
                # If the bot stayed up for a while, reset the backoff. Otherwise it
                # crashed fast — grow the delay so we don't spin.
                wait_now, delay = restart_backoff(
                    delay,
                    minimum=RESTART_DELAY_MIN,
                    maximum=RESTART_DELAY_MAX,
                    healthy=ran_for >= 60,
                )
                say(
                    log,
                    f"supervisor: bot exited rc={rc} after {ran_for:.0f}s "
                    f"(attempt {attempt}); restarting in {wait_now}s"
                )
                try:
                    time.sleep(wait_now)
                except KeyboardInterrupt:
                    say(log, "\nsupervisor: Ctrl+C — stopping")
                    return 0
        finally:
            # OS 在行程結束時本來就會放掉，這裡只是明確收尾——順帶讓「lock 必須活到
            # 這裡」這件事在程式碼上看得出來，不會被當成沒用的變數清掉。
            lock.release()
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
