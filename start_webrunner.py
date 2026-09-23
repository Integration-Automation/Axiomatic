"""Supervisor launcher for the axiomatic generation schedule.

Runs ``axiomatic/webrunner_novelai.py`` (selenium variant, default) or
``axiomatic/webrunner_je_only.py`` (je variant) in a supervised loop:
if the webrunner exits non-zero (Chrome crash, chromedriver transport
stall, unhandled exception), the launcher waits an exponential backoff
and respawns. rc=0 (todo finished cleanly) exits the loop. Ctrl+C stops
cleanly.

Mirrors the bot's `_watch_for_fallback` supervisor:

- exponential backoff (`respawn_backoff_min_sec` → `respawn_backoff_max_sec`)
- reset backoff to min if the previous run lasted ≥ `healthy_threshold_sec`
- rapid-fail giveup: after `rapid_fail_giveup_count` consecutive crashes
  with `alive_for < rapid_fail_threshold_sec`, the launcher exits instead
  of looping forever (prevents the "Chrome can't init → infinite respawn
  loop redoing first-time login" scenario).
- zero-progress giveup: after `zero_progress_giveup_count` consecutive runs
  that exit rc=3 (attempted generation, saved nothing), the launcher stops.
  This is the SLOW counterpart of rapid-fail: when generation is blocked,
  each run burns the full `consecutive_fail_abort` budget before dying, so
  it never looks "rapid" and the rapid-fail gate never fires.
- rc=4 (generation blocked — a purchase / account dialog): stop immediately,
  no respawn. Retrying cannot help; a fresh Chrome sees the same dialog.

All six parameters come from `bot_config.json` → `webrunner_supervisor`
via the shared `_bot_config.load_bot_config()` loader, so this launcher
and the bot stay in sync — edit the config once, both supervisors pick
it up next start.

Network loss is not a crash (2026-09-22, mirroring the bot supervisor): when
the webrunner exits non-zero, the launcher first asks ``_connectivity`` whether
the host can reach the internet at all. If it clearly cannot, that exit does
NOT count toward the rapid-fail / zero-progress give-up gates — the launcher
waits (with no time limit; only Ctrl+C or a stop command ends the wait) until
the network is back, then respawns at once and the run resumes from its
per-character checkpoint. Without this, a forty-minute outage trips a give-up
gate and nothing restarts the batch when the network returns.

The webrunner subprocess is started with the interpreter ``python_command()``
picks — the repo-local ``.venv`` first, then the Windows ``py -3`` launcher,
then the interpreter that started this launcher (DoD #5 in ``CLAUDE.md``) —
so it doesn't matter which Python you used to invoke this script. Under the
``.venv`` interpreter every program shows up as **two** processes with the
same command line: CPython's venv redirector and the real interpreter under
it. That is expected, not a duplicate launch (the launcher's own
§8.5), and ``webrunner.pid`` holding the outer one is correct.

Note: don't run this AND the bot's `!run` at the same time — both spawn
the webrunner and would race for the Chrome profile lock. 每次 spawn 之前會
先讀 `webrunner.pid` 判斷是否已有批次在跑，有的話就讓位；**那個檔案讀不出來
時同樣不啟動**——判不出來一律走保守的那一邊（見 `_live_webrunner_pid`）。

Usage:
    py -3 start_webrunner.py              # selenium variant (default)
    py -3 start_webrunner.py selenium     # explicit selenium
    py -3 start_webrunner.py je           # je variant (je_web_runner)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Share supervisor params with the bot. Same JSON file, same defaults —
# editing `bot_config.json` once affects both this launcher and the bot's
# in-process `_watch_for_fallback` supervisor. `_bot_config` / `_supervisor`
# 都是被動共用模組（CLAUDE.md 允許的第三通道）。
#
# **走套件路徑 `axiomatic.x`，不要用 `sys.path.insert` ＋ 裸名匯入。**
# 兩種寫法執行期都會動（跑腳本時 Python 會把**腳本所在目錄**放進 `sys.path[0]`，
# 而這支就住在 repo root，所以 `axiomatic` 這個套件一定找得到，跟當下的工作目錄
# 無關）。差別在裸名版本**只有執行期才成立**：靜態分析器、IDE、linter 看不到那個
# 執行期才發生的 `sys.path.insert`，於是把 `from _bot_config import …` 標成
# unresolved import——`noqa` 註記（E402 那一類）就是為了壓這種噪音才長出來的。
# 這一行刻意不寫成真正的指示詞形式：ruff 會把註解裡任何長成那樣的字串當成一道
# 壞掉的指示詞，每跑一次就吐一則 warning，而那正好會蓋掉真的打錯的那一則。
# `start_discord_bot.py` 從一開始就是套件路徑，這裡跟它對齊，兩支啟動器只留一種
# 匯入風格。
from axiomatic import _chrome_slot
from axiomatic import _connectivity
from axiomatic._bot_config import load_bot_config
from axiomatic._run_progress import atomic_write_text
from axiomatic._supervisor import (
    RC_ZERO_PROGRESS,
    acquire_single_instance_lock,
    other_launcher_pids,
    restart_backoff,
    say,
    stream_child,
    trim_log,
    webrunner_exit_needs_human,
)

REPO_ROOT = Path(__file__).resolve().parent
AXIOMATIC_DIR = REPO_ROOT / "axiomatic"
SCRIPTS = {
    "selenium": AXIOMATIC_DIR / "webrunner_novelai.py",
    "je": AXIOMATIC_DIR / "webrunner_je_only.py",
}

# 子行程的主控台輸出**同時**落地成檔案。用的是 bot 側 `/run` 已經在寫的同一個
# `webrunner.log`，所以不管批次是誰起的，`/log tail`／`/log grep`／`/log size`
# 都查得到同一份紀錄。
#
# 為什麼一定要有這個：接續判定失敗時，`_webrunner_shared.run_batch` 會印出
# 決定性的 `resume: checkpoint … does NOT match …; Diverging fields:` ＋ 每個
# 欄位的 stored/current，但這支啟動器原本是 `subprocess.run(cmd)`——子行程直接
# 繼承主控台，關掉視窗那行就永遠找不回來了（2026-08-23 實測踩到）。
#
# 差別：bot 每次 spawn 都把這個檔**清空**（一次 `/run` 一份），啟動器則是
# **附加**——會炸掉的正是「崩潰 → 重生」那條接縫，清掉就等於把要查的東西丟了。
# growth 由 `_trim_log` 以尾段保留法封頂。
WEBRUNNER_LOG = REPO_ROOT / "webrunner.log"
LOG_MAX_BYTES = 8 * 1024 * 1024     # 超過就修剪
LOG_KEEP_BYTES = 4 * 1024 * 1024    # 修剪後保留的尾段

# 單一實例鎖。`start_discord_bot.py` 從 2026-08-30 就有一把，這一支沒有——不對稱
# 純屬遺漏，而**這一側的後果更重**：bot 本體自己還有第二把鎖擋著，webrunner 沒有
# 任何一層。兩個批次監督者同時在跑的話，兩邊各自 spawn 一個 webrunner，兩個
# webrunner 搶同一份 `.chrome_profile/`、各自 nuclear sweep 把對方的 chrome 殺掉、
# 各自從同一組 `todo_*.md` 取件，而且兩邊看起來都「正常」。
#
# 為什麼現在補：2026-09-03 要接開機自動啟動，那條路一定會遇到「使用者手動開著、
# 開機任務又開一個」。沒有這把鎖的話，自動啟動等於把偶發的雙開變成常態。
#
# 鎖檔與 bot 那兩把刻意分開（一支程式一個鎖檔，見 `acquire_single_instance_lock`
# 的 docstring）。鎖由 OS 持有，行程一消失就自動釋放，`taskkill /F` 也一樣，所以
# 沒有「殘留鎖檔擋住下次啟動」這回事。
LOCK_FILE = REPO_ROOT / ".webrunner_supervisor.lock"

# 跨行程「同機只有一個 Chrome stack」協定（見 `axiomatic/_chrome_slot.py`）。
# webrunner 一啟動就會 nuclear sweep——**無條件殺光所有 `chrome.exe`**——所以
# 「sweep ＋ spawn」必須在持槽期間發生，否則會殺掉正在跑的驗證瀏覽器；反過來，
# 批次跑起來之後靠 `webrunner.pid` 當長期訊號，驗證端讀到活著的 pid 就讓位。
# 這支啟動器過去兩件都沒做，等於整條不變量在啟動器路徑上不存在。
SLOT_OWNER = "launcher"
# 驗證腳本自己的總時限約 240s，所以等到 300s 還拿不到就不是「剛好排在驗證中間」，
# 而是有人卡住了——`_chrome_slot` 的行程存活判定會即時回收死掉的持有者，等這麼久
# 仍失敗代表需要人看一眼。
SLOT_ACQUIRE_TIMEOUT_SEC = 300.0
WEBRUNNER_PID_FILE = REPO_ROOT / "webrunner.pid"


def _save_pid(pid: int) -> None:
    """寫 `webrunner.pid`（原子；跨行程被讀的檔一律如此——半寫入會被讀成別的
    pid 或空字串，而讀的人不會知道自己讀錯了）。

    寫的是 `Popen` 拿到的 pid。`.venv/Scripts/python.exe` 是轉接殼，真正的
    webrunner 是它的子行程；但轉接殼會一直等到子行程結束才退出，所以拿它當
    **存活訊號**是正確的（驗證端只問「還活著嗎」）。要**終止**才需要找真正那個
    孫行程——那是 bot `_find_all_webrunner_pids` 的工作，不是這裡。"""
    atomic_write_text(WEBRUNNER_PID_FILE, str(pid))


def _live_webrunner_pid() -> tuple[int | None, bool]:
    """讀 `webrunner.pid`。回 `(pid, 判定得出來嗎)`。

    與 `verify_browser._live_webrunner_pid` 同語意、同形狀——那支是「有批次在跑
    就讓位、不開瀏覽器」，這支是「有批次在跑就不要再起一個」。剩下的 pid 檔若指向
    死掉的行程就當它不存在（正常收尾會自己清掉，被硬殺時才會留下）。

    **第二個回傳值不是裝飾品。** 原本檔案存在但讀不出內容時一律回 `None`，而
    `None` 在呼叫端的意思是**「沒有批次在跑，可以起一個」**——判不出來卻走了樂觀
    的那一邊，結果是同一台機器上跑起第二個批次：兩個 webrunner 搶同一份
    `.chrome_profile/`、各自 nuclear sweep 把對方的 Chrome 殺掉、從同一組
    `todo_*.md` 重複取件，而兩邊的紀錄看起來都正常。這個函式的存在理由就是擋這
    件事。CLAUDE.md 的 Windows PID 存活硬規則對這一類判斷寫得很明白：**凡是在問
    「我該不該不要啟動／讓位？」的地方，判不出來就要走保守的那一邊**。底層的
    `_chrome_slot._pid_alive` 早就是保守的（判不出來回 True），錯的一直是上面這層
    讀檔。

    三種情況要分清楚：

    * 檔案**不存在** → 真的沒有批次 → `(None, True)`，可以起。
    * 檔案**存在但讀不出來**（權限、IO 錯誤、內容不是合法 UTF-8）→ **判不出來**
      → `(None, False)`，呼叫端不得啟動。
    * 檔案存在、讀得出來但不是數字（含空字串）→ 同上，判不出來。

    `UnicodeDecodeError` 要單獨列：它是 `ValueError` 的子類、**不是** `OSError`，
    所以原本的 `except (FileNotFoundError, OSError)` 接不到它，內容不是合法 UTF-8
    時會直接往上炸穿這個函式。
    """
    if not WEBRUNNER_PID_FILE.exists():
        return None, True                    # 沒有檔案＝真的沒有批次在跑
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, True                    # 剛好在這一瞬間被刪掉，等同不存在
    except (OSError, UnicodeDecodeError):
        return None, False                   # 檔案在、但讀不出來 → 判不出來
    try:
        pid = int(raw)
    except ValueError:
        return None, False                   # 內容不是數字 → 判不出來
    return (pid if _chrome_slot._pid_alive(pid) else None), True


def _clear_pid_if_ours(pid: int) -> None:
    """只有當 pid 檔還記著**我們寫進去的那個 pid** 時才刪。

    寫同一個檔的現在有**三個**：bot、本檔，以及（2026-09-11 起）沒有父行程發布
    訊號時自己認領的 webrunner（`_webrunner_shared.claim_liveness_signal`）。
    無條件刪會把別人的存活訊號清掉，驗證端就會在批次還在跑的時候開第二個瀏覽器
    搶同一份登入 profile。Best-effort，永不 raise。

    webrunner 那一側用的是**同一個判準**（「檔案還記著我寫的那個 pid 嗎」），所以
    兩邊誰先寫都不會誤刪對方——父行程是在 `Popen` 回來之後才寫的，子行程理論上有
    機會搶先。

    「永不 raise」是這裡宣告的契約，所以例外要接**全**：`UnicodeDecodeError` 是
    `ValueError` 的子類、不是 `OSError`，原本的 `except (FileNotFoundError,
    OSError)` 接不到它——pid 檔內容不是合法 UTF-8 時它會從這裡炸出去，而這支是在
    `finally` 裡被呼叫的，等於用一個清理路徑的例外蓋掉子行程真正的結束原因。
    方向本來就是對的（讀不出來就不刪，寧可留下別人的存活訊號）。"""
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return
    if raw != str(pid):
        return
    try:
        WEBRUNNER_PID_FILE.unlink()
    except OSError:
        pass


def _network_is_up() -> bool:
    """主機連得上網路嗎（判準見 `_connectivity`；判不出來偏向「連得上」）。"""
    return _connectivity.is_online()


def _wait_for_network() -> float:
    """等到網路回來，回傳等了幾秒。沒有上限；Ctrl+C 的 `KeyboardInterrupt` 照常往外丟。"""
    return _connectivity.wait_until_online(
        poll_sec=_connectivity.DEFAULT_POLL_SEC)


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Supervisor launcher for the axiomatic webrunner."
    )
    parser.add_argument(
        "variant",
        nargs="?",
        default="selenium",
        choices=sorted(SCRIPTS.keys()),
        help="Which webrunner variant to launch (default: selenium).",
    )
    args = parser.parse_args()
    script = SCRIPTS[args.variant]
    if not script.exists():
        print(f"missing {script}", file=sys.stderr)
        return 1

    sup = load_bot_config()["webrunner_supervisor"]
    backoff_min = float(sup["respawn_backoff_min_sec"])
    backoff_max = float(sup["respawn_backoff_max_sec"])
    healthy_sec = float(sup["healthy_threshold_sec"])
    rapid_threshold_sec = float(sup["rapid_fail_threshold_sec"])
    rapid_giveup = int(sup["rapid_fail_giveup_count"])
    zero_progress_giveup = int(sup["zero_progress_giveup_count"])

    # `_bot_config` coerces each backoff value to a positive number
    # *independently*, so a config with max < min loads fine and only blows up
    # later, inside `restart_backoff`'s ValueError guard — i.e. the supervisor
    # dies with an unhandled traceback the first time the webrunner crashes,
    # exactly when it is supposed to take over. Fail fast at startup instead,
    # before anything is spawned.
    if backoff_max < backoff_min:
        print(
            f"bad webrunner_supervisor config: respawn_backoff_max_sec "
            f"({backoff_max:.0f}) < respawn_backoff_min_sec "
            f"({backoff_min:.0f}); fix bot_config.json and rerun.",
            file=sys.stderr,
        )
        return 1

    cmd = python_command() + ["-u", str(script)]
    # 附加模式：跨「崩潰 → 重生」與跨啟動器重啟都要留得住，那正是要查的接縫。
    # 開檔前先修剪，所以封頂發生在每次啟動，不會在長時間執行中途搬檔。
    trim_log(WEBRUNNER_LOG, max_bytes=LOG_MAX_BYTES,
             keep_bytes=LOG_KEEP_BYTES)
    try:
        log = WEBRUNNER_LOG.open("a", encoding="utf-8", buffering=1)
    except OSError as error:
        # 記錄檔開不起來不該擋住整批產圖——降級成「只有主控台」，照舊往下跑。
        print(f"supervisor: cannot open log ({error!r}); console only",
              file=sys.stderr)
        log = None

    # 鎖要綁在區域變數上並活到 return，但**理由不是「被回收就會放掉」**——
    # `InstanceLock` 刻意沒有 `__del__`（本機實測；
    # `test_supervisor.test_instance_lock_must_not_grow_a_del_method`
    # 在守），所以丟掉參照只是洩漏一個 fd，鎖仍然握著到行程結束。留著這個名字是
    # 為了讓「收尾」在程式碼上看得出來。**不要**因為讀到舊的那句說法就去補一個
    # `__del__`：補完之後才會真的長出它警告的那個缺陷——呼叫端只要沒把它存進變數，
    # 鎖就被靜默放掉，第二個監督者當場起得來。
    lock = acquire_single_instance_lock(LOCK_FILE)
    if lock is None:
        others = other_launcher_pids(Path(__file__).name)
        detail = f"（既有 pid: {', '.join(map(str, others))}）" if others else ""
        say(log, f"supervisor: 已經有另一個批次監督者在執行{detail}，這次不啟動。",
            err=True)
        say(log, "supervisor: 要改跑這一個的話，先結束既有的那個再重新啟動。",
            err=True)
        return 1
    if lock.degraded:
        # 降級不是錯誤，但**一定要講出來**：拿不到互斥保護卻照常啟動是刻意的
        # 取捨（見 `acquire_single_instance_lock`），而無聲的降級等於根本沒有
        # 這把鎖。這一側後果更重——兩個批次監督者同時跑會變成兩個 webrunner 搶
        # 同一份設定檔目錄、互相 nuclear sweep，而兩邊的紀錄看起來都正常。
        # 兩支啟動器的措辭刻意一致。
        say(log, "supervisor: 注意——這台機器上的單一實例保護沒有生效，"
                 "這次不會擋掉重複啟動，請自己確認只開了一個。")

    try:
        return _supervise(cmd, args.variant, log, backoff_min=backoff_min,
                          backoff_max=backoff_max, healthy_sec=healthy_sec,
                          rapid_threshold_sec=rapid_threshold_sec,
                          rapid_giveup=rapid_giveup,
                          zero_progress_giveup=zero_progress_giveup)
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                pass


def _supervise(cmd: list[str], variant: str, log, *, backoff_min: float,
               backoff_max: float, healthy_sec: float,
               rapid_threshold_sec: float, rapid_giveup: int,
               zero_progress_giveup: int) -> int:
    """spawn → 等 rc → 判斷要不要重生的主迴圈。抽出來只為了讓 `main()` 能用
    `try/finally` 保證記錄檔關得掉；政策本身一行都沒動。"""
    say(log, f"supervisor launching ({variant}): {' '.join(cmd)}")
    backoff = backoff_min
    rapid_fails = 0
    # 連續「零產出」輪數。與 rapid-fail 是兩道**互補**的閘：rapid-fail 抓的是
    # 「死得太快」（Chrome 根本起不來），這道抓的是「死得很慢卻什麼都沒產出」
    # ——生成被擋住時每一輪都要跑完 consecutive_fail_abort 才結束，遠遠超過
    # rapid_fail_threshold_sec，所以那道閘永遠不會響，監督者就無限重生下去。
    zero_progress = 0
    attempt = 0
    while True:
        attempt += 1
        # **單調時鐘，不是牆鐘。** 這個值只用來量「子行程活了多久」，從不離開本
        # 行程、不寫檔、也不跟任何檔案 mtime 比對——判準見 `_chrome_slot.acquire`
        # 的 docstring。`time.time()` 會被 NTP 的 step 修正、手動改時鐘、虛擬機
        # 快照還原跳動（換時區與日光節約時間**不會**，那個回的是 UTC epoch 秒），
        # 而這個值餵的是 rapid-fail giveup，兩個方向都會壞：往回撥 → 跑得好好的
        # 子行程被算成 rapid fail、監督者提早放棄；往前撥 → 看起來活很久、計數被
        # 重置，監督者**無限重生**一個真的壞掉的子行程。無人值守的機器上第二種
        # 特別糟——「放棄並留下紀錄」變成「安靜地一直重試」。
        start = time.monotonic()
        say(log, f"==== attempt {attempt}: spawning webrunner ====")
        # 跨行程 Chrome 槽的短臨界區：**取槽 → spawn → 寫 pid → 放槽**。
        # webrunner 起來的第一件事就是 nuclear sweep（殺光所有 chrome.exe），
        # 所以那一段一定要在持槽期間；pid 寫好之後槽就可以放掉，之後改由
        # `webrunner.pid` 當長期訊號讓驗證端讓位（見 `_chrome_slot` 的順序契約）。
        if not _chrome_slot.acquire(SLOT_OWNER,
                                    timeout=SLOT_ACQUIRE_TIMEOUT_SEC,
                                    label=f"{variant}-attempt-{attempt}"):
            say(log,
                f"supervisor: chrome slot still held after "
                f"{SLOT_ACQUIRE_TIMEOUT_SEC:.0f}s; refusing to spawn (a "
                f"verification run, or a stuck lock). Nothing was swept and "
                f"nothing was started; resolve it and rerun.",
                err=True)
            return 1
        # 持槽之後才問「是不是已經有批次在跑」——這樣看到的是一致的狀態。
        # 有的話就讓位：再起一個的下場是新的 webrunner 開場 nuclear sweep 把對方的
        # Chrome 殺掉，兩個監督者互打、`/stop` 與 `/status` 又只看得到其中一個。
        # 死掉的行程留下的 pid 檔不算（`_live_webrunner_pid` 會判存活）。
        other, decided = _live_webrunner_pid()
        if other is not None:
            _chrome_slot.release(SLOT_OWNER)
            say(log,
                "supervisor: another generation run is already alive "
                "(webrunner.pid); standing aside instead of starting a second "
                "browser stack. Stop that one first, then rerun.",
                err=True)
            return 1
        if not decided:
            # pid 檔在、但讀不出內容（權限／IO／不是合法 UTF-8）：**判不出來就不要
            # 啟動**。這裡樂觀一次的代價是整台機器上跑起第二個批次——兩個 webrunner
            # 搶同一份 profile、互相 sweep、重複取件；相對地保守一次的代價只是使用者
            # 要去看一眼那個檔案。與槽被佔住那條一樣不做重試迴圈：檔案讀不出來重試
            # 一百次還是讀不出來，這需要人處理。
            _chrome_slot.release(SLOT_OWNER)
            say(log,
                "supervisor: webrunner.pid 存在但讀不出內容（權限／IO／不是合法 "
                "UTF-8），無法判定是否已有批次在跑；保守起見不啟動。確認沒有批次在"
                "跑之後把那個檔案刪掉再重跑。",
                err=True)
            return 1
        spawned: list[int] = []

        def _on_spawn(proc: "subprocess.Popen") -> None:
            # 先寫 pid 再放槽——順序反過來會出現「槽空了、pid 還沒寫」的空窗，
            # 驗證端剛好在那一瞬間取槽就會判定沒人在跑、開出第二個 Chrome stack。
            _save_pid(proc.pid)
            spawned.append(proc.pid)
            _chrome_slot.release(SLOT_OWNER)

        try:
            rc = stream_child(cmd, log, cwd=str(REPO_ROOT),
                              on_spawn=_on_spawn,
                              pump_name="webrunner-log-pump")
        except KeyboardInterrupt:
            say(log, "supervisor: Ctrl+C — stopping")
            return 0
        finally:
            # spawn 失敗（Popen 丟例外／`_on_spawn` 沒跑到）也要把槽放掉，
            # 否則自己把自己鎖住到 staleness 逾時。release 只在本行程仍持有時
            # 刪檔，所以重複呼叫是安全的。
            _chrome_slot.release(SLOT_OWNER)
            # 子行程已經結束（或被 Ctrl+C 收掉）→ 收回存活訊號，不然驗證端會
            # 以為批次還在跑而一直讓位。
            if spawned:
                _clear_pid_if_ours(spawned[0])
        alive_for = time.monotonic() - start   # 單調時鐘，見上面 `start`
        # 站方擋住生成（購買／方案資訊）：重生只會看到同一個對話框，還會每輪重跑
        # 一次登入 ＋ setup。直接停下來等人處理。
        if webrunner_exit_needs_human(rc):
            say(
                log,
                f"supervisor: webrunner stopped after {alive_for:.0f}s because "
                f"generation is blocked and retrying cannot help (rc={rc}). "
                f"Resolve it in the browser, then rerun. The queue and the "
                f"resume checkpoint are untouched.",
                err=True,
            )
            return 1
        if rc == 0:
            say(
                log,
                f"supervisor: webrunner finished cleanly after "
                f"{alive_for:.0f}s (attempt {attempt}); exiting"
            )
            return 0
        # 明確斷網：這一次失敗**不計入**兩道放棄閘，也不走退避——等網路回來就立刻
        # 重生，批次會從檢查點逐角色接續。與 bot 監督者（`_watch_for_fallback` 的
        # Case N）同一個判準、同一個順序：排在「需要人處理」之後（那是批次在頁面
        # 上看到的狀況，網路當時是通的），排在計數之前。等待沒有上限——擁有者裁定
        # 只有明確的停止（Ctrl+C、`/stop`、`/launcher stop`）能結束它。
        if not _network_is_up():
            say(
                log,
                f"supervisor: webrunner exited rc={rc} after {alive_for:.0f}s "
                f"while the network is down (attempt {attempt}); not counting "
                f"it toward the give-up limits. Waiting for the network to "
                f"come back, then resuming from the checkpoint.",
                err=True,
            )
            try:
                waited = _wait_for_network()
            except KeyboardInterrupt:
                say(log, "supervisor: Ctrl+C — stopping")
                return 0
            say(log, f"supervisor: the network is back after {waited:.0f}s; "
                     f"restarting now")
            # 計數一起歸零，與 bot 那一側一致（它接續時是一輪全新的 `/run`，計數
            # 本來就從零開始）。斷網的開頭常常是幾次「網路時好時壞、探測剛好探到
            # 通」的失敗，它們已經被算進去了；不歸零的話，網路回來後的第一次正常
            # 失敗就可能直接觸發放棄。
            backoff = backoff_min
            rapid_fails = 0
            zero_progress = 0
            continue
        # Wait at the current delay, then grow the delay for the next crash.
        healthy = alive_for >= healthy_sec
        wait_now, backoff = restart_backoff(
            backoff,
            minimum=backoff_min,
            maximum=backoff_max,
            healthy=healthy,
        )
        if healthy:
            rapid_fails = 0
        # 零產出計數：連續幾輪「嘗試過生成卻一張都沒存」。重生一次是對的（壞掉的
        # 是那個 session），連續幾輪都這樣就代表重生解決不了。
        if rc == RC_ZERO_PROGRESS:
            zero_progress += 1
            if zero_progress >= zero_progress_giveup:
                say(
                    log,
                    f"supervisor: {zero_progress} consecutive runs produced "
                    f"zero images; giving up instead of respawning. Something "
                    f"needs a human — check the log and the browser, then "
                    f"rerun.",
                    err=True,
                )
                return 1
        else:
            zero_progress = 0
        # Rapid-fail giveup runs independently of backoff: even if backoff
        # has grown to max, the launcher still gives up after enough quick
        # crashes in a row instead of churning forever.
        if alive_for < rapid_threshold_sec:
            rapid_fails += 1
            if rapid_fails >= rapid_giveup:
                say(
                    log,
                    f"supervisor: {variant} crashed {rapid_fails} times "
                    f"in a row within {rapid_threshold_sec:.0f}s each; "
                    f"giving up. Common causes: Chrome can't init "
                    f"(see `chromedriver.log`), `.chrome_profile/` lockfile, "
                    f"missing config. Fix and rerun.",
                    err=True,
                )
                return 1
        else:
            rapid_fails = 0
        suffix = (
            f" (rapid-fail {rapid_fails}/{rapid_giveup})"
            if rapid_fails > 0 else ""
        )
        if zero_progress > 0:
            suffix += f" (zero-progress {zero_progress}/{zero_progress_giveup})"
        say(
            log,
            f"supervisor: webrunner exited rc={rc} after {alive_for:.0f}s "
            f"(attempt {attempt}); restarting in {wait_now:.0f}s{suffix}"
        )
        try:
            time.sleep(wait_now)
        except KeyboardInterrupt:
            say(log, "supervisor: Ctrl+C — stopping")
            return 0


if __name__ == "__main__":
    sys.exit(main())
