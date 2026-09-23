"""跨行程的「行程探查 ＋ 終止」原語（bot 側）。

P4 重構：把 `discord_bot.py` 裡**無狀態**的行程操作抽出來。判準與 P1/P2 相同——
「純函式／只進不出的行程操作」搬出，「跟 bot runtime 狀態（webrunner 子行程
handle、`_webrunner_stop_requested` 旗標、產圖佇列、discord channel、asyncio
task）糾纏的監督迴圈」留在 bot。

所以**真正的 supervisor 迴圈**（`_watch_for_fallback` / `_do_webrunner_run` /
`_spawn_webrunner` / `_spawn_oneshot_webrunner` / `_reap_oneshot_webrunner` /
`cmd_run` / `cmd_stop`）仍住在 `discord_bot.py`——它們透過 `global` 改寫 8 個 bot
模組層全域、又跟產圖佇列／discord channel 深度耦合，硬搬出去只會逼出循環 import
或把每個 `_webrunner_proc` 變成 `state.proc` 的脆弱抽象，反而更難讀。本模組只收
那些「給定參數就能算、完全不碰 bot 全域」的葉子函式。

模組邊界（CLAUDE.md 硬規則）：這是 **bot 側模組**，可被 bot import；它**不可**
`import discord_bot`（循環），也**不可** `from webrunner_novelai import …`
（bot↔webrunner 只透過磁碟檔溝通）。本模組純 stdlib ＋ 選用 psutil，無此風險。

不洩漏規則：本模組回給呼叫端的 status lines 都是 bot 自行撰寫、known-safe 的泛
用字串（「已停止背景程式」「sweep 殺光所有 chrome：…」），不含服務名／本機路徑／
PID／原始例外。PID 等診斷細節一律 `print(..., file=sys.stderr)`，不進回傳值、不進
Discord。新增字串時務必維持這個分界。
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
from pathlib import Path


# ---------- PID liveness / kill primitives ---------------------------------

def _nt_pid_alive(pid: int) -> bool:
    """Windows 專用的存活探測——**只查詢、不送任何訊號**。psutil 優先（本專案
    requirements.txt 已把 psutil 列為必要相依）；真的缺了才退回 ctypes 的
    ``OpenProcess`` ＋ ``GetExitCodeProcess``。無法判定時回 True（保守）。"""
    try:
        import psutil  # type: ignore
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    except Exception:  # pylint: disable=broad-except
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        # PROCESS_QUERY_LIMITED_INFORMATION：夠查存活，且不需要 terminate 權限。
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED(5) ＝ 行程存在但無權限查詢 → 視為活著。
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pylint: disable=broad-except
        return True


def _pid_alive(pid: int) -> bool:
    """PID 是否還活著。**純探測，不得對目標造成任何副作用。**

    Windows 上**不能**用 ``os.kill(pid, 0)`` 當探測：``signal.CTRL_C_EVENT == 0``，
    所以 CPython 的 Windows ``os.kill`` 會把 signal 0 導向
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``——那是把 ``pid`` 當成
    **console process group id**、對共用 console 的整組行程送出真正的 Ctrl+C，
    而且對絕大多數 pid 會直接失敗丟 OSError。舊寫法因此在 Windows 上兩頭都錯：
    (a) 活著的 webrunner pid 通常不是共用 console 的 process group leader →
    OSError → 回報「已死」，害 `_load_pid` 砍掉有效的 PID 檔、`_webrunner_alive()`
    誤判成沒在跑；(b) 萬一 pid 真的對上某個 console process group，這個「探測」
    會送 Ctrl+C 打斷它。``ProcessLookupError`` 在 Windows 也幾乎不會被丟出，那條
    分支等同死碼。故 Windows 走 `_nt_pid_alive`，POSIX 才保留 ``os.kill(pid, 0)``。
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _nt_pid_alive(int(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it
    except OSError:
        return False
    return True


def _kill_by_pid(pid: int) -> bool:
    """Try graceful then forceful kill. Returns True if the process is gone."""
    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        print(f"SIGTERM pid={pid} failed: {error!r}", file=sys.stderr)
    return False


# `taskkill` 的等待上限。實測單次約 30-80ms（見下方 sweep 段落的註解），所以 15 秒
# 是「絕對不正常」而不是「有點慢」。
#
# **為什麼需要上限。** `taskkill` 不是永遠會回來的：目標行程卡在核心的不可中斷等待
# （典型就是顯示驅動的 I/O）時，`/F` 也得排隊等它。而長時間跑瀏覽器的機器**長期**有顯示驅動
# 層當機的紀錄，Chrome 又是吃 GPU 的——「一個 chrome.exe 卡在 GPU 驅動裡」正是這條
# 路會遇到的情況。呼叫端已經用 `asyncio.to_thread(...)` 把它丟出 event loop，所以
# heartbeat 不會死；死的是**那條執行緒**，而 `_terminate_all_webrunner_instances`
# 在 await 它——於是 `!stop` / `!restart` 永遠不回覆，而且「重生前先清乾淨」這一步
# 再也走不完。症狀是安靜地卡住，跟「現在沒事發生」分不出來。
_TASKKILL_TIMEOUT_SEC = 15.0

# 殺完 chrome 之後等作業系統真的放掉 profile 的檔案握柄，下一次 selenium 才鎖得到。
#
# **抽成常數不是為了可調，是為了測試不必真的睡。** 這條路的 11 支測試餵的是假行程，
# 但同一行 `sleep` 照樣睡滿——合計 22 秒，佔 `test_process_control.py` 三十幾秒裡的
# 大半，而且對這個模組跑變異時每個變異都再付一次。測試檔用 autouse 夾具把它設成 0；
# `test_the_chrome_release_wait_is_still_a_real_wait_in_production` 直接讀原始碼的
# 字面值釘住「正式值 >= 1 秒」，所以「為了讓測試快一點把正式值也改成 0」會當場紅——
# 那個夾具看不到、也改不到原始碼裡的數字。
_CHROME_RELEASE_WAIT_SEC = 2


def _run_taskkill(args: list[str]) -> bool:
    """跑一次 `taskkill`，**有上限**，成功與否都不往外拋。回「有沒有跑完」。

    三個呼叫端原本各自寫一次 `subprocess.run([...], capture_output=True,
    check=False)`，其中 `_force_kill_pid` 的 Windows 分支**連 try 都沒有**——它的
    docstring 寫著「錯誤 swallow」，但 `check=False` 只吞非零結束碼，吞不掉
    spawn 階段的 `OSError`（例如 PATH 上找不到 `taskkill`）。同一條規則三份實作，
    修好的永遠只有其中一份，所以收成這裡一份。

    ⚠️ **`subprocess.TimeoutExpired` 不是 `OSError`**（MRO 是
    `TimeoutExpired → SubprocessError → Exception`，本機實測確認）。也就是說只加
    `timeout=` 而不擴大 except，會把「安靜地卡住」換成「例外往外炸」——那是把一個
    缺陷換成另一個。兩種都要接。
    """
    try:
        subprocess.run(  # nosec B603 - 固定命令，pid 是自己掃出來的整數
            args, capture_output=True, check=False,
            timeout=_TASKKILL_TIMEOUT_SEC,
        )
        return True
    except subprocess.TimeoutExpired:
        # `run(timeout=...)` 到期時會先把子行程殺掉才拋，所以這裡不會留孤兒。
        print(f"taskkill timed out after {_TASKKILL_TIMEOUT_SEC:.0f}s: "
              f"{' '.join(args)}", file=sys.stderr)
        return False
    except OSError as error:
        # `!r` 而不是 `{error}`：`OSError` 的 str 會帶出檔案路徑（本專案的洩漏規則）。
        print(f"taskkill failed: {error!r}", file=sys.stderr)
        return False


def _force_kill_pid(pid: int) -> None:
    """OS-specific 強制終止；錯誤 swallow（process 可能已死）。"""
    if os.name == "nt":
        _run_taskkill(["taskkill", "/F", "/PID", str(pid)])
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------- process discovery (psutil scans) -------------------------------

def _find_all_chrome_processes() -> tuple[list[tuple[int, str]], bool]:
    """掃所有 `chrome.exe` / `chromedriver.exe`，**不分** cmdline 是否含
    我們的 `.chrome_profile/`。

    為什麼這麼狠：Chrome 是 multi-process（主 process + N 個 renderer +
    GPU + utility 等等）。原本只殺「cmdline 含 .chrome_profile 的」漏
    網率還是高 —
    (a) 部分 child process 的 cmdline 不會帶 `--user-data-dir`，只帶
        `--type=renderer` 之類的子 flag；
    (b) parent 被殺後 child 會被 reparent 到 system process，psutil 的
        cmdline 偶爾就 expire 拿不到完整參數；
    (c) Chrome 自己 race condition 留下的 utility process 跟 webrunner
        毫無 visible 關聯但會拿著 profile lock。

    使用者明確要求「執行前關掉所有 chrome」，所以無條件殺。MSEdge 不在
    名單裡（user 平常用 Edge 瀏覽、不應影響）；如果哪天有其他正常 Chrome
    流程同時要保留，這條規則要再加白名單。

    **回傳 `(名單, 掃描是否完整)`——第二個值不是裝飾品，呼叫端一定要用。**
    原本只回名單，於是「掃描一開始就炸」跟「機器上真的沒有 chrome」回的都是
    `[]`，**呼叫端無法區分**。實測（2026-09-07，假的 psutil 注入）兩者送給使用者
    的訊息一字不差：都是什麼都不說、什麼都不殺。使用者以為清乾淨了，實際上一個
    都沒殺，下一輪 `webdriver.Chrome(...)` 就撞上舊的 singleton lock。
    掃到一半才炸更糟：回傳**部分**名單，而呼叫端照樣宣稱「殺光所有 chrome」。

    `test_psutil_facade.py` 的開頭在 2026-08-30 就把這個症狀寫下來了
    （「`_find_all_chrome_processes()` 回 `[]` → nuclear sweep 什麼都不做」），
    但它守的是**其中一個成因**（psutil API 漂移）。權限不足、WMI 卡住、列舉途中
    的暫時性 OS 錯誤都會走到同一個 `except Exception`，而那些靜態守門看不到。
    所以要守的是**後果**：掃描不完整這件事本身必須送得出去。

    注意 `except (NoSuchProcess, AccessDenied)` 那個內層 `continue` **不算**掃描
    失敗——行程在列舉途中消失是這個 API 的日常，把它當失敗會讓警告天天出現，而
    天天出現的警告等於沒有警告。
    """
    targets = ("chrome.exe", "chromedriver.exe")
    found: list[tuple[int, str]] = []
    try:
        import psutil  # type: ignore
    except ImportError:
        return [], False
    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if pid == own_pid:
                    continue
                name = (proc.info.get("name") or "").lower()
                if name in targets:
                    found.append((pid, name))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 單一行程在列舉途中消失／取不到權限是**常態**，不是掃描失敗——
                # 跳過它，整份名單仍然算完整。
                continue
    except Exception as error:  # pylint: disable=broad-except
        print(f"_find_all_chrome_processes failed: {error!r}",
              file=sys.stderr)
        return found, False
    return found, True


def collapse_interpreter_stub_pairs(
        raw: list[tuple[int, int, str]],
        exclude_pid: int | None = None) -> list[tuple[int, str]]:
    """把「轉接殼 ＋ 本尊」併成一筆，並排掉 `exclude_pid` 那一整對。

    輸入是 `[(pid, ppid, script), …]`，輸出是 `[(pid, script), …]`。

    **為什麼需要這個**：Windows 上的 venv（virtualenv 與 stdlib `venv` 都一樣，
    放的都是 CPython 自己的 `venvlauncher.exe`）裡，
    `.venv\\Scripts\\python.exe` 不是直譯器本體而是一個**轉接殼**——它用同一份
    命令列 spawn base 直譯器當子行程，然後**活到子行程結束**才把 rc 轉回去。
    於是每一個經由 `sys.executable` 啟動的背景程式，在 cmdline 掃描裡**永遠是
    兩筆**：pid 不同、cmdline 一模一樣、起始時間同一秒、而且是父子關係。
    不併的話，一個正常執行中的背景程式會被永久誤報成「多了一個孤兒行程」
    （`!health` / `!doctor` 都吃這個結果）。實測見

    規則是「父行程也在命中清單裡就丟掉」，也就是**保留最外層那一筆**——
    `Popen.pid` 拿到的正是它，而終止它會連子行程一起帶走（`taskkill /T` 會，
    實測 `Popen.terminate()` 也會，轉接殼把子行程放在同一個 job 裡）。

    `exclude_pid` 兩半都認：呼叫端手上的可能是外層（`Popen.pid`）也可能是內層
    （背景程式自己寫進 pid 檔的 `os.getpid()`），少認一半就等於少排除一筆。
    """
    hits = {pid for pid, _ppid, _script in raw}
    excluded: set[int] = set()
    if exclude_pid:
        excluded.add(exclude_pid)
        for pid, ppid, _script in raw:
            if ppid and ppid == exclude_pid:
                excluded.add(pid)   # exclude_pid 是外層轉接殼
            if pid == exclude_pid and ppid:
                excluded.add(ppid)  # exclude_pid 是內層本尊
    return [
        (pid, script) for pid, ppid, script in raw
        if pid not in excluded and ppid not in hits
    ]


# ---------- "是不是這支腳本" 的判定 ----------------------------------------
#
# 這幾個 helper 存在的理由是**誤判的代價不對稱**：判定結果會被拿去 `taskkill /F`。
# 少殺一個漏網的背景產圖程式，下一次 `/gen run` 會再掃一次；多殺一個，使用者的
# 編輯器／shell／git 就沒了。

# 背景產圖程式必然是 Python 行程。名稱涵蓋 Windows 的 `python.exe`／`pythonw.exe`／
# `py.exe`（`py -3` 那條路徑會多一層轉接殼）與 POSIX 的 `python3`／`python3.14`。
_PY_INTERPRETER_RE = re.compile(
    r"^(?:python|pythonw|py)[0-9]*(?:\.[0-9]+)*(?:\.exe)?$", re.IGNORECASE)


def looks_like_python_process(name) -> bool:
    """行程名看起來是不是 Python 直譯器。"""
    if not isinstance(name, str):
        return False
    return bool(_PY_INTERPRETER_RE.match(name.strip()))


# 這幾個 flag 會**吃掉下一個參數**，所以那一格不是腳本路徑。
_PY_FLAGS_TAKING_A_VALUE = frozenset({"-X", "-W", "--check-hash-based-pycs"})


def python_script_argument(cmdline) -> str | None:
    """這個 Python 行程真正**在執行**的腳本檔路徑；`-c` / `-m` / 讀 stdin 回 None。

    Python 的命令列形狀是 `[直譯器, *直譯器flag, 腳本, *腳本自己的參數]`，這裡只認
    「腳本」那一格。**為什麼不能只看「有沒有哪個參數指向那支檔案」**：腳本自己的
    參數也可能就是我們在找的檔名，而那代表完全相反的意思——

        python -m black   axiomatic/webrunner_novelai.py    ← 在**格式化**那支檔案
        python -m pyflakes axiomatic/webrunner_novelai.py   ← 在**檢查**那支檔案
        python -u          axiomatic/webrunner_novelai.py   ← 在**執行**那支檔案

    前兩個被判成「漏網的背景產圖程式」的話，下一次 `/gen run` 會把使用者正在跑的
    格式化工具 `taskkill /F` 掉。只有第三個才是。
    """
    args = list(cmdline or [])
    index = 1                      # [0] 是直譯器自己
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str):
            return None
        if arg == "-":             # `python -` ＝ 從 stdin 讀程式，沒有腳本檔
            return None
        if not arg.startswith("-"):
            return arg             # 第一個非 flag ＝ 腳本
        if arg in _PY_FLAGS_TAKING_A_VALUE:
            index += 2
            continue
        if arg.startswith("--"):
            index += 1
            continue
        for char in arg[1:]:       # 合併寫法：`-uB`、`-um`、`-Xdev`
            if char in "cm":
                return None        # `-c` / `-m` ⇒ 這次根本沒有腳本檔
            if char in "XW":
                break              # `-Xdev` 後面接的是值，不是別的 flag
        index += 1
    return None


def cmdline_runs_script(cmdline, targets) -> str | None:
    """這個命令列是不是**在執行**這些腳本之一；是就回那個檔名。

    兩道關卡：先由 `python_script_argument` 取出「真正在執行的那一格」，再比對
    路徑最後一段。所以絕對路徑、相對路徑、以及 `cd` 進去用裸檔名啟動都認得，而
    任何只是**提到**檔名的命令列都不算——

    - `git commit -m "webrunner_novelai.py: 修好對話框"`
    - `code D:/.../webrunner_novelai.py`（編輯器，殺掉＝未存檔的內容沒了）
    - `grep -n webrunner_novelai.py -r .`
    - `python -m black axiomatic/webrunner_novelai.py`

    2026-08-30 在這台機器上實測：舊的子字串比對掃出 **7** 個「背景產圖程式」，
    其中只有 2 個是真的；另外 5 個是命令列裡剛好提到檔名的 shell 與 `python -c`。
    當時要是有人下 `/gen stop`／`/gen run`，那 5 個會被 `taskkill /F` 掃掉。
    改成這個判定之後掃出 2 個，正好是那兩個真的。

    比對前把反斜線正規化成正斜線、並轉小寫（Windows 路徑不分大小寫）。
    """
    script = python_script_argument(cmdline)
    if not isinstance(script, str):
        return None
    norm = script.replace("\\", "/").strip().strip('"').lower()
    for target in targets:
        low = target.lower()
        if norm == low or norm.endswith("/" + low):
            return target
    return None


# ---------- 「跑著的是不是磁碟上的那一份」 ---------------------------------
#
# 2026-09-01 補。起因是擁有者問「為什麼功能沒生效」，而答案是行程比程式碼舊四天。
# 這個專案的每一個診斷都在回答「有沒有在跑」，沒有一個回答「跑著的跟磁碟上的是不是
# 同一份」。Python 在 import 期就把模組綁定完成，所以編輯磁碟對活著的行程毫無影響，
# 而**症狀是「功能像是沒寫」**——最容易讓人回頭去重讀原始碼、卻永遠讀不出問題。
#
# 當時實測的四個進入點**全部**是舊的，其中 supervisor 落後自己的啟動器四天，
# 後果是 `discord_bot.log` 根本不存在，而 bot 的錯誤回覆一直在叫人「查看 log」。
# 順帶一提：重啟 bot 子行程**不會**重載 supervisor，那是另一個長命行程。

# 進入點 → (腳本檔名, 它會載入、因此改了非重啟不可的本專案檔案)。
# 執行期才讀的檔案（設定檔、佇列）不算，那些改了立刻生效。
#
# **判準是「這個行程的 `sys.modules` 裡會不會有它」，不是「模組層有沒有 import 它」。**
# 函式裡的延遲 import 一樣會被快取，綁的一樣是**第一次載入時**那一份原始碼，所以
# 改了同樣要重啟。這張表因此就是各進入點的**傳遞 import 閉包**。
#
# **這張表 2026-09-09 之前是手寫的，而且四筆有三筆不對**，方向都是最糟的那一種：
#
# * `bot` 少了七個模組（`_webrunner_shared`、`discord_rpc`、`_run_progress`、
#   `_batch_config`、`_chrome_slot`、`_code_fingerprint`、`_supervisor`）——
#   改了那些檔案，健康報告會說 bot 是新的。
# * `batch supervisor` 只列了兩個，少了四個。
# * `batch` 列著 `_chrome_slot.py`，而 `webrunner_novelai.py` **根本不 import 它**
#   （import 它的是 `start_webrunner.py`）。**那一筆掛錯元件**：改 `_chrome_slot`
#   會誤報批次陳舊，同時漏報真的該重啟的批次監督者。
#   ⚠️ **這一筆在 2026-09-11 反轉了，而且是被全套測試抓到的**：
#   `_webrunner_shared.py` 為了認領存活訊號加了一行 `import _chrome_slot`，於是它
#   **真的**進了批次的傳遞閉包，那一行又被加回去（見下面表裡的註解）。留著這段
#   歷史是因為它說明了為什麼這張表不能手寫：**觸發條件是「多一條 import 邊」，
#   不是「多一個模組」**——在既有共用模組裡加一行 import，不會讓任何人想到要回來
#   改這張表，而四個元件裡某幾個的正確答案已經變了。
#
# 少列會讓 `find_stale_components` 安靜地漏報，而多列會讓它指著一個無關的檔名說
# 「就是這個」——`find_stale_components` 回傳的 `dep_rel` 是要給人看的。兩個方向
# 都會誤導，所以現在由
# `test_process_control.test_the_component_map_matches_the_real_import_closure`
# 用 AST 算出真正的閉包、兩個方向對帳。**要改這張表，先讓那支測試同意。**
STALE_COMPONENTS = {
    "supervisor": ("start_discord_bot.py", (
        "start_discord_bot.py",
        "axiomatic/_supervisor.py",
        "axiomatic/_process_control.py",
    )),
    "bot": ("discord_bot.py", (
        "axiomatic/discord_bot.py",
        "axiomatic/dorossi_backend.py",
        "axiomatic/_bot_config.py",
        "axiomatic/_help_strings.py",
        "axiomatic/_external_apis.py",
        "axiomatic/_gui_control.py",
        "axiomatic/_bot_prompts.py",
        "axiomatic/_process_control.py",
        "axiomatic/_queue_consume.py",
        "axiomatic/presence_probe.py",
        "axiomatic/_batch_config.py",
        "axiomatic/_chrome_slot.py",
        "axiomatic/_code_fingerprint.py",
        "axiomatic/_run_progress.py",
        "axiomatic/_supervisor.py",
        "axiomatic/_webrunner_shared.py",
        "axiomatic/discord_rpc.py",
        "axiomatic/_warn_dedup.py",
        "axiomatic/_power_request.py",
        "axiomatic/_connectivity.py",
    )),
    "batch supervisor": ("start_webrunner.py", (
        "start_webrunner.py",
        "axiomatic/_supervisor.py",
        "axiomatic/_bot_config.py",
        "axiomatic/_chrome_slot.py",
        "axiomatic/_process_control.py",
        "axiomatic/_run_progress.py",
        "axiomatic/_warn_dedup.py",
        "axiomatic/_connectivity.py",
    )),
    "batch": ("webrunner_novelai.py", (
        "axiomatic/webrunner_novelai.py",
        "axiomatic/_webrunner_shared.py",
        "axiomatic/_batch_config.py",
        "axiomatic/_queue_consume.py",
        "axiomatic/_run_progress.py",
        "axiomatic/_code_fingerprint.py",
        "axiomatic/_process_control.py",
        "axiomatic/_supervisor.py",
        # 2026-09-11 補回來。2026-09-09 這一筆是被**刪掉**的，理由是「批次根本不
        # import 它，import 它的是 `start_webrunner.py`」——那在當時是對的。同日
        # 稍晚 `_webrunner_shared.py` 為了認領存活訊號加了 `import _chrome_slot`，
        # 於是它又進了批次的**傳遞**閉包。全套測試抓到了（漏列那個方向），而這正是
        # 這張表要由閉包對帳、不能手寫的理由：一個共用模組多一行 import，就會靜靜
        # 改掉四個元件裡某幾個的答案。
        "axiomatic/_chrome_slot.py",
        "axiomatic/_warn_dedup.py",
    )),
}


def newest_dependency_mtime(root: Path, rel_paths, *, mtime=None):
    """`rel_paths` 裡最新的 mtime 與它是哪一個；全部不存在回 `(0.0, "")`。

    `mtime` 可注入（回 float 或 None ＝檔案不存在），讓判定邏輯測得起來而不必
    在測試裡造真的檔案。永不 raise——這是診斷用的，壞掉不該影響呼叫端。"""
    if mtime is None:
        def mtime(rel):                       # noqa: E306
            try:
                return (root / rel).stat().st_mtime
            except OSError:
                return None
    best, who = 0.0, ""
    for rel in rel_paths:
        try:
            got = mtime(rel)
        except Exception:  # pylint: disable=broad-except
            got = None
        if isinstance(got, (int, float)) and not isinstance(got, bool) \
                and got > best:
            best, who = float(got), rel
    return best, who


# ---------- 「主機當掉之後，這一套自己回得來嗎」 -----------------------------
#
# 2026-09-05 補。`install_autostart.py` 已經把兩支監督者註冊成**登入時**觸發的
# 排程工作，所以「主機重開之後沒人把東西拉起來」那一半解決了——**但只在有人登入
# 的前提下**。一台長期無人值守的主機每隔幾天就可能自動重開，
# 而重開後如果停在鎖定畫面，登入觸發永遠不會發生：2026-09-02 20:20 那次就是這樣
# 停了大約 23 小時。
#
# 為什麼不乾脆改成開機觸發：批次要開一個真的有桌面的 Chrome，開機觸發跑在
# session 0 沒有互動桌面，Chrome 起不來。要真正做到「重開就自己回來」，需要在
# 系統層開自動登入（`AutoAdminLogon`）——那會把帳號密碼放進主機的認證存放區，
# 屬於主機安全設定，不該由程式偷偷改掉。
#
# 所以這裡只做**偵測**：把「鏈路是完整的還是缺一角」講清楚，讓它在 `/sys doctor`
# 上看得見，而不是等下一次當機才發現。
_AUTOSTART_TASKS = (r"\Axiomatic\Bot", r"\Axiomatic\Batch")

# 「沒有指定」的哨符。**不能用 `None`**：`None` 在這裡是有意義的值（「判斷不出來」），
# 拿它兼作「沒指定」的話，測試想注入「判斷不出來」就會被當成「請你自己去查」，
# 於是真的跑去讀登錄檔——注入形同無效。2026-09-05 實測踩到。
_UNSET = object()

_WINLOGON_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
_SYSTEM_POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def _autologon_enabled() -> bool | None:
    """主機有沒有開自動登入。`None` ＝ 判斷不出來（非 Windows／讀不到）。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINLOGON_KEY)
        try:
            raw, _kind = winreg.QueryValueEx(key, "AutoAdminLogon")
        finally:
            winreg.CloseKey(key)
        # **刻意精確比對，不 `.strip()`、不收 `"true"`。** 依據不是「Winlogon 一定
        # 只認 `"1"`」——那不重開機驗不出來——而是這支探測該往哪一側犯錯：寬鬆的
        # 那一側會讓 `/sys doctor` 那句「這台主機沒有開自動登入」**安靜消失**，而
        # 那句話是這支函式存在的唯一理由。犯錯成本是不對稱的：多印一行 NOTE vs.
        # 一台停在鎖定畫面沒人知道的主機（2026-09-02 實測停了約 23 小時）。
        # `str()` 要留著——REG_DWORD 讀回來是 `int`，見那支測試的 `(1, True)` 格。
        return str(raw) == "1"
    except FileNotFoundError:
        return False          # 值不存在 ＝ 沒開
    except OSError:
        return None
    except Exception:  # pylint: disable=broad-except
        return None


# 自動登入「現在開著」不代表「下一次重開還開著」（2026-09-19 實測）。Windows Update
# 重開時的「自動登入並鎖定」（ARSO）會自己設一套 `AutoLogonCount`；照文件，那個值
# 歸零時 Winlogon 會把 `AutoAdminLogon` 改成 0、刪掉 `DefaultPassword`——**連擁有者
# 自己設好的那一份一起清**。這台主機的「驗證 UI」事件記錄有 8 筆 Id 5013「由於登錄值
# AutoLogonCount 達到零，自動登入設定已移除」，**每一筆都落在 Windows Update 兩段式
# 重開的第二次開機那一秒**；使用者自己按的重開與冷開機一次都沒有。所以只看
# `AutoAdminLogon` 的探測會一路回報鏈路完整，直到下一次更新把它清掉、主機又停在
# 鎖定畫面——那正是這整段偵測要防的失效，而它會以「一切正常」的樣子抵達。
def _autologon_expiry() -> str | None:
    """開著的自動登入會不會自己消失。回原因鍵；`None` ＝不會，或判斷不出來。

    * `"count_limited"`  — Winlogon 底下有 `AutoLogonCount`：每次自動登入減一，
      歸零就整組清掉。**只看存不存在、不看值**——值是 0 也是「下一次開機就清」。
    * `"update_restart"` — ARSO 沒被原則關掉。原則值**不存在＝預設開**（文件原文：
      "If you don't configure this policy setting, it's enabled by default"），所以
      值不存在要回這個鍵，不是 `None`。

    只有 **REG_DWORD 的 1** 算「已關」。文件載明的型別是 DWORD，字串型的 `"1"`
    Windows 認不認不重開驗不出來；與 `_autologon_enabled` 同一個取捨——寬鬆的那一側
    會讓警告**安靜消失**，嚴格的那一側最多多印一行 NOTE。

    兩個都有問題時報 `count_limited`：它下一次開機就生效，比等更新近。
    判斷不出來（讀不到、非 Windows）一律回 `None`：只有確定的壞消息才值得對人講。
    永不 raise。
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        def _read(path, name):
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
            try:
                return winreg.QueryValueEx(key, name)
            finally:
                winreg.CloseKey(key)

        try:
            _read(_WINLOGON_KEY, "AutoLogonCount")
            return "count_limited"
        except FileNotFoundError:
            pass
        try:
            raw, kind = _read(_SYSTEM_POLICY_KEY, "DisableAutomaticRestartSignOn")
        except FileNotFoundError:
            return "update_restart"
        if kind == winreg.REG_DWORD and raw == 1:
            return None
        return "update_restart"
    except OSError:
        return None
    except Exception:  # pylint: disable=broad-except
        return None


# 第三個缺口：擁有者**自己按的**重新啟動會被常駐程式否決（2026-09-19 實測）。從開始
# 功能表按重新啟動，兩個常駐程式擋下關機（Winsrv 10001），60 秒後 Winlogon 4004／
# User32 1073「重新啟動失敗」、回到鎖定畫面——主機根本沒重開，自動登入沒機會跑，從
# 外面看卻像「重開了但自動登入沒反應」，是另一個問題的樣子。更新造成的重開會強制
# 關程式，所以只有手動重開會中。擁有者裁定後設了目前使用者的 `AutoEndTasks`（REG_SZ
# `"1"`）：關機時由結束程式的 CSRSS 元件讀，不必重新登入就生效。這支讓偵測端**看得到**
# 那個值——哪天被清掉（重灌、設定工具、別的程式改回去），不必等下一次手動重開失敗才
# 發現。
_DESKTOP_KEY = r"Control Panel\Desktop"


def _auto_end_tasks_enabled() -> bool | None:
    """關機時會不會自動結束擋路的程式（目前使用者的 `AutoEndTasks`）。

    `None` ＝判斷不出來（非 Windows／讀不到）。讀的是 **HKCU**，不是其他幾支的
    HKLM——這個值是逐使用者的，而會按下重新啟動的就是跑這個行程的那個使用者
    （bot 與批次都由該使用者登入時的排程工作拉起）。

    **只有 REG_SZ 的 `"1"` 算開**，與 `_autologon_enabled` 同一個取捨：這支該往哪一
    側犯錯。文件載明的型別是字串；REG_DWORD 的 1、帶空白的 `"1 "` 關機時認不認，不真
    的重開驗不出來。寬鬆的那一側會讓 `/sys doctor` 那句提醒**安靜消失**，嚴格的那一側
    最多多印一行 NOTE。值不存在（預設 0）回 False；永不 raise。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _DESKTOP_KEY)
        try:
            raw, kind = winreg.QueryValueEx(key, "AutoEndTasks")
        finally:
            winreg.CloseKey(key)
        return kind == winreg.REG_SZ and raw == "1"
    # 順序是承重的：`FileNotFoundError` 是 `OSError` 的子類別，兩個對調之後
    # 「值不存在」會被讀成「判斷不出來」，於是這句提醒在唯一該講的情形安靜消失。
    except FileNotFoundError:
        return False          # 值不存在 ＝ 預設 0 ＝ 會被擋
    except OSError:
        return None
    except Exception:  # pylint: disable=broad-except
        return None


def autostart_recovery_status(*, tasks=None, query=None,
                              autologon=_UNSET, expiry=_UNSET,
                              auto_end_tasks=_UNSET) -> dict:
    """主機當掉→重開→這一套能不能自己回來。回一個小 dict，永不 raise。

    * `registered` — 已註冊的排程工作名稱
    * `missing`    — 應該有卻沒註冊的
    * `autologon`  — True / False / None（判斷不出來）
    * `expiry`     — 自動登入**確定開著**時，它會不會自己消失（見
                     `_autologon_expiry`）；沒開或判斷不出來時一律是 `None`
    * `auto_end_tasks` — 自動登入**確定開著**時，手動重開會不會自動結束擋路的程式
                     （見 `_auto_end_tasks_enabled`）；沒開或判斷不出來時一律是 `None`
    * `gap`        — `None` ＝鏈路完整；否則是一句「缺哪一角」的說明鍵。由重到輕：
                     `not_registered` ＞ `needs_logon` ＞ `autologon_expires` ＞
                     `restart_vetoable`

    `tasks` / `query` / `autologon` / `expiry` / `auto_end_tasks` 都可注入，所以整段
    判定測得起來而不必真的去動工作排程器或登錄檔。
    """
    names = _AUTOSTART_TASKS if tasks is None else tuple(tasks)
    if query is None:
        def query(name):                      # noqa: E306
            try:
                done = subprocess.run(  # nosec B603 B607 — 固定命令
                    ["schtasks", "/Query", "/TN", name],
                    capture_output=True, text=True,
                    encoding="oem" if os.name == "nt" else "utf-8",
                    errors="replace", check=False, timeout=20)
                return done.returncode == 0
            except Exception:  # pylint: disable=broad-except
                return None
    registered, missing, unknown = [], [], []
    for name in names:
        try:
            got = query(name)
        except Exception:  # pylint: disable=broad-except
            got = None
        if got is True:
            registered.append(name)
        elif got is False:
            missing.append(name)
        else:
            unknown.append(name)
    auto = _autologon_enabled() if autologon is _UNSET else autologon
    # 只有「確定開著」才問它會不會消失：沒開的話 `needs_logon` 已經講了更嚴重的事，
    # 而在沒開的主機上說「開著但會被清掉」是自相矛盾。
    expires = None
    # 同一個理由：只有「確定開著」才問手動重開會不會被擋。沒開的話重開成不成功都
    # 回不來，講這個只是雜訊；判斷不出來時本來就該沉默。
    end_tasks = None
    if auto is True:
        expires = _autologon_expiry() if expiry is _UNSET else expiry
        end_tasks = (_auto_end_tasks_enabled() if auto_end_tasks is _UNSET
                     else auto_end_tasks)
    gap = None
    if missing:
        gap = "not_registered"
    elif auto is False:
        # 註冊了，但觸發條件是「登入時」，而主機不會自己登入。
        gap = "needs_logon"
    elif expires is not None:
        # 現在登得進去，但下一次（更新）重開就會被清掉：鏈路完整，只是有期限。
        gap = "autologon_expires"
    elif end_tasks is False:
        # 最輕的一角：當機重開、更新重開都回得來，只有擁有者自己按的重開可能被
        # 常駐程式擋下而根本沒重開。**只認 `is False`**——`None`（判斷不出來）要沉默。
        gap = "restart_vetoable"
    return {"registered": registered, "missing": missing, "unknown": unknown,
            "autologon": auto, "expiry": expires, "auto_end_tasks": end_tasks,
            "gap": gap}


def find_stale_components(root: Path, components=None, *, procs=None,
                          mtime=None):
    """哪些正在跑的元件比它自己的程式碼舊？

    回 `[(label, pid, started, dep_mtime, dep_rel), …]`，依「落後多久」由多到少排序。
    空 list ＝ 跑著的每一個元件都是磁碟上的那一份。

    `procs` 是 `(pid, name, cmdline, create_time)` 的可迭代物，`mtime` 是
    `rel -> float | None`；兩個都可注入，所以整段判定邏輯不需要真的行程或真的檔案
    就測得起來。預設用 psutil；沒有 psutil 就回空 list（診斷缺席，不是報錯）。

    **轉接殼會被一起列出來**，這是刻意的：`.venv\\Scripts\\python.exe` 與它 spawn
    出來的真直譯器都跑同一個腳本、啟動時間相同，兩個都舊。呼叫端要顯示給人看的話
    自己去重（見 `collapse_interpreter_stub_pairs`）。

    永不 raise。"""
    components = STALE_COMPONENTS if components is None else components
    if procs is None:
        try:
            import psutil  # type: ignore
        except ImportError:
            return []
        def _iter():                          # noqa: E306
            for proc in psutil.process_iter(attrs=["pid", "name", "create_time"]):
                try:
                    yield (proc.info.get("pid"), proc.info.get("name"),
                           proc.cmdline(), proc.info.get("create_time"))
                except Exception:  # pylint: disable=broad-except
                    continue
        procs = _iter()

    wanted = {}
    for label, (script, deps) in components.items():
        dep_mtime, dep_rel = newest_dependency_mtime(root, deps, mtime=mtime)
        wanted[script] = (label, dep_mtime, dep_rel)

    stale = []
    for pid, name, cmdline, started in procs:
        if not looks_like_python_process(name):
            continue
        try:
            hit = cmdline_runs_script(cmdline, tuple(wanted))
        except Exception:  # pylint: disable=broad-except
            continue
        if not hit:
            continue
        label, dep_mtime, dep_rel = wanted[hit]
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            continue
        if dep_mtime > 0 and started < dep_mtime:
            stale.append((label, pid, float(started), dep_mtime, dep_rel))
    stale.sort(key=lambda row: row[3] - row[2], reverse=True)
    return stale


def _find_all_webrunner_pids(
        exclude_pid: int | None = None) -> tuple[list[tuple[int, str]], bool]:
    """psutil 掃所有 process，找 cmdline 內含 `webrunner_novelai.py` 或
    `webrunner_je_only.py` 的 — 不論是不是 bot 啟動的、也不論是哪個 bot
    session 啟動的。給 `cmd_stop` 用來保證殺光所有實例（漏網的、手動
    `start_webrunner.py` 啟動的、上次 bot crash 留下的孤兒 process 等等）。

    **回傳 `(名單, 掃描是否完整)`——第二個值不是裝飾品，呼叫端一定要用。**
    原本只回名單，於是「掃描一開始就炸」跟「機器上真的沒有漏網的」回的都是
    `[]`，**呼叫端無法區分**。實測（2026-09-07，注入假的 psutil）兩者送給
    使用者的訊息一字不差：`/sys health` 都說「⏸️ not running」、`/sys doctor`
    都說「no obvious blocker」。而那兩句話的後果差很多——掃不成代表 sweep
    不會殺任何東西，於是**兩個背景產圖程式同時搶同一個瀏覽器設定檔**，登入態
    與佇列都會被弄壞。掃到一半才炸更糟：回傳**部分**名單，而呼叫端照樣把
    「沒有更多了」當成事實。

    注意 `except (NoSuchProcess, AccessDenied)` 那個內層 `continue` **不算**
    掃描失敗——行程在列舉途中消失是這個 API 的日常，把它當失敗會讓警告天天
    出現，而天天出現的警告等於沒有警告。（範本與更長的理由見同模組的
    `_find_all_chrome_processes`。）

    回傳前會過 `collapse_interpreter_stub_pairs`：一個邏輯上的背景程式只回一筆，
    不會因為 virtualenv 的轉接殼而算成兩個（見該函式）。

    `exclude_pid` 用於避免 `!kill` 之類的 user 介入時誤殺 bot 自己；這邊
    `os.getpid()` 已經內建排除（bot 自己 cmdline 是 discord_bot.py）。

    **命中條件是「Python 行程」＋「某個參數就是那支腳本的路徑」**，兩個都要成立。
    只看 cmdline 子字串會把任何**提到**檔名的命令列一起算進來（見
    `cmdline_runs_script`），而這份清單的下游是 `taskkill /F`。

    **掃描順序也是刻意的**：先用 `attrs=["pid", "name"]` 過濾，再只對通過的那幾筆
    個別讀 `cmdline()` 與 `ppid()`。psutil 在 Windows 上的 `Process.ppid()` 實作是
    `ppid_map()[self.pid]`，而 `ppid_map()` 每次呼叫都重建**整台機器**的對照表——
    寫成 `attrs=[..., "ppid"]` 就是 N 個行程各掃一次全系統快照，複雜度 O(N²)。
    本機實測（361 個行程）：

    | 取法 | 耗時 |
    |---|---|
    | `attrs=["pid", "name"]` | 118 ms |
    | `attrs=["pid", "ppid"]` | 3,721 ms |
    | `attrs=["pid", "ppid", "cmdline"]`（舊寫法）| 4,188 ms |
    | 本函式現在的寫法 | **140 ms** |

    也就是說貴的是 `ppid` 而不是 `cmdline`（`cmdline` 只佔約 0.4 秒）。這條路徑會被
    `/sys health`、`/sys doctor` 與 `_failure_diagnostic_summary` 直接呼叫，省下來的
    4 秒是實實在在的。"""
    targets = ("webrunner_novelai.py", "webrunner_je_only.py")
    raw: list[tuple[int, int, str]] = []
    try:
        import psutil  # type: ignore
    except ImportError:
        return [], False
    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if pid == own_pid:
                    continue
                if not looks_like_python_process(proc.info.get("name")):
                    continue
                matched = cmdline_runs_script(proc.cmdline(), targets)
                if matched:
                    raw.append((pid, proc.ppid() or 0, matched))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 單一行程在列舉途中消失／取不到權限是**常態**，不是掃描失敗——
                # 跳過它，整份名單仍然算完整。
                continue
    except Exception as error:  # pylint: disable=broad-except
        print(f"_find_all_webrunner_pids failed: {error!r}", file=sys.stderr)
        # 已經掃到的留住（能殺一個是一個），但誠實說這份名單不完整。
        return collapse_interpreter_stub_pairs(raw, exclude_pid), False
    return collapse_interpreter_stub_pairs(raw, exclude_pid), True


# ---------- blocking sweep steps (run via asyncio.to_thread) ---------------
#
# 這三個 helper 都是**同步且會明顯阻塞**的：Windows 上每個 pid 要 spawn 一次
# `taskkill`（單次約 30-80ms，40 個 chrome 行程就是 1.5-3 秒）。
# `_terminate_all_webrunner_instances` 是 coroutine、跟 discord.py 的 heartbeat
# 共用同一個 event loop，所以呼叫端一律用 `asyncio.to_thread(...)` 丟到執行緒，
# 不可直接在 event loop 上跑（見該函式 docstring）。

def _signal_webrunner_pids(
        survivors: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """對每個 survivor 送出終止訊號，回「實際送出的」清單。"""
    terminated: list[tuple[int, str]] = []
    for spid, script in survivors:
        try:
            if os.name == "nt":
                # Windows 沒 SIGTERM；先 try graceful taskkill /T，再 /F。
                _run_taskkill(["taskkill", "/PID", str(spid), "/T"])
            else:
                os.kill(spid, signal.SIGTERM)
            terminated.append((spid, script))
        except OSError:
            pass
    return terminated


def _force_kill_survivors(
        pids: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """對仍活著的 pid 逐一強制終止，回「實際被強制終止的」清單。"""
    forced: list[tuple[int, str]] = []
    for spid, script in pids:
        if _pid_alive(spid):
            _force_kill_pid(spid)
            forced.append((spid, script))
    return forced


def _kill_chrome_pids(chrome_procs: list[tuple[int, str]]) -> None:
    """無條件強制殺掉掃到的 chrome / chromedriver。"""
    for cpid, _cname in chrome_procs:
        try:
            if os.name == "nt":
                _run_taskkill(["taskkill", "/PID", str(cpid), "/T", "/F"])
            else:
                os.kill(cpid, signal.SIGKILL)
        except OSError:
            pass


# ---------- combined sweep -------------------------------------------------

async def _terminate_all_webrunner_instances(
        tracked_proc, active_pid) -> list[str]:
    """殺光所有 webrunner 實例 — tracked Popen、adopted PID、跟 psutil
    掃出來的 orphans。回一份 status lines 給 caller 拼進 reply 訊息。

    **狀態用參數注入**（P4）：`tracked_proc` 是 bot 目前追蹤的 `Popen`（即
    `_webrunner_proc`，沒有就傳 None）、`active_pid` 是 bot 算好的目前 pid
    （即 `_active_pid()` 的結果，沒有就傳 None）。本函式只讀這兩個參數、不碰
    任何 bot 全域；呼叫者（`cmd_stop` / `cmd_run` / `_spawn_oneshot_webrunner`
    / `mcmd_abort`）負責在呼叫前後 reset in-memory state（`_webrunner_proc` /
    `_webrunner_pid` / `_webrunner_variant`）跟清 `webrunner.pid`。

    被 `cmd_stop` 跟 `cmd_run` 共用：`cmd_run` 開頭也跑一次這個，所以
    就算機器上已有漏網 instance（user 自己用 `start_webrunner.py` 跑
    的、上次 bot crash 留下的孤兒、或 supervisor race 內 spawn 的）
    都會在新 instance 起來前清掉，避免兩個 webrunner 同時搶 Chrome
    profile / 寫 events.ndjson。

    **絕不可在 event loop 上做阻塞呼叫**：本 coroutine 跟 discord.py 的 gateway
    heartbeat 共用同一個 event loop。單次 `!stop` 的阻塞成本很可觀——
    `Popen.wait` 最久 10 秒、`psutil.process_iter` 掃全機（chrome 那輪還掃兩次；
    成本主要在 `ppid`——psutil 在 Windows 上每取一次 ppid 就重建一次全系統對照表，
    細節見 `_find_all_webrunner_pids`）、每個 pid 一次
    `taskkill` spawn（40 個 chrome 行程約 1.5-3 秒）。加總輕易超過 10 秒，
    heartbeat 送不出去就會先噴 "Heartbeat blocked" 警告、再被 gateway 判失聯而
    斷線重連。所以每一段阻塞工作都包成同步 helper 再用 `asyncio.to_thread`
    丟到執行緒；新增清掃步驟時請維持這個規則。
    """
    lines: list[str] = []

    # 1. 殺已知的 tracked / adopted process（如果有）。
    pid = active_pid
    if pid is not None:
        if tracked_proc is not None and tracked_proc.poll() is None:
            tracked_proc.terminate()
            try:
                # 把 timeout 交給 Popen.wait 自己（在執行緒裡），逾時仍照舊丟
                # subprocess.TimeoutExpired，且不會留下卡住的背景執行緒。
                await asyncio.to_thread(tracked_proc.wait, 10)
                print(f"terminate: stopped tracked pid={pid}", file=sys.stderr)
                lines.append("已停止背景程式")
            except subprocess.TimeoutExpired:
                tracked_proc.kill()
                print(f"terminate: force-killed pid={pid} (didn't exit in 10s)",
                      file=sys.stderr)
                lines.append("已強制結束背景程式")
        else:
            _kill_by_pid(pid)
            for _ in range(20):  # wait up to 10s
                if not _pid_alive(pid):
                    break
                await asyncio.sleep(0.5)
            if _pid_alive(pid):
                await asyncio.to_thread(_force_kill_pid, pid)
                print(f"terminate: force-killed adopted pid={pid}", file=sys.stderr)
                lines.append("已強制結束背景程式")
            else:
                print(f"terminate: stopped adopted pid={pid}", file=sys.stderr)
                lines.append("已停止背景程式")

    # 2. psutil 掃漏網的：未被 tracked 的 webrunner_*.py python process。
    #    SIGTERM → 等 5s → 仍活就 SIGKILL。
    survivors, wr_scan_ok = await asyncio.to_thread(_find_all_webrunner_pids)
    if survivors:
        terminated = await asyncio.to_thread(_signal_webrunner_pids, survivors)
        for _ in range(10):
            if not any(_pid_alive(p) for p, _ in terminated):
                break
            await asyncio.sleep(0.5)
        forced = await asyncio.to_thread(_force_kill_survivors, terminated)
        if terminated:
            print("terminate sweep: "
                  + ", ".join(f"pid {p} ({s})" for p, s in terminated),
                  file=sys.stderr)
            lines.append(f"sweep 清掉 {len(terminated)} 個漏網的背景程式實例")
        if forced:
            print("terminate sweep force-killed: "
                  + ", ".join(f"pid {p}" for p, _ in forced), file=sys.stderr)
            lines.append(f"其中 {len(forced)} 個以 SIGKILL 強制結束")

    # 3. 殺所有 chrome.exe / chromedriver.exe（無條件、nuclear option）。
    #    User 明確要求「執行前關掉所有 chrome」— 原本依 `.chrome_profile/`
    #    cmdline 過濾的 targeted sweep 漏網率高（Chrome multi-process 的
    #    renderer / GPU / utility 不一定帶 user-data-dir flag）。
    chrome_procs, scan_ok = await asyncio.to_thread(_find_all_chrome_processes)
    if chrome_procs:
        await asyncio.to_thread(_kill_chrome_pids, chrome_procs)
        # 等幾秒讓 OS 釋放 profile 的 file handle、下次 selenium 才 lock 得到。
        await asyncio.sleep(_CHROME_RELEASE_WAIT_SEC)
        # 再掃一次看有沒有殺成功
        survivors, rescan_ok = await asyncio.to_thread(
            _find_all_chrome_processes)
        still_alive = [p for p, _ in survivors]
        # 按 chrome.exe vs chromedriver.exe 分類報告
        n_chrome = sum(1 for _, n in chrome_procs if n == "chrome.exe")
        n_driver = sum(1 for _, n in chrome_procs if n == "chromedriver.exe")
        # 掃描不完整時不得宣稱「殺光所有」——那句話的保證來自掃描的完整性，
        # 而不是來自我們殺了幾個。少講一句，好過講一句做不到的保證。
        head = "殺光所有 chrome" if scan_ok else "殺掉掃得到的 chrome"
        lines.append(
            f"sweep {head}：{n_chrome} 個 chrome.exe + "
            f"{n_driver} 個 chromedriver.exe = {len(chrome_procs)} 個"
        )
        if still_alive:
            print(f"terminate: chrome survivors pids={still_alive[:20]}",
                  file=sys.stderr)
            lines.append(
                f"⚠️ 仍有 {len(still_alive)} 個 chrome process 殺不掉 "
                f"(可能 antivirus 鎖住 handle)"
            )
        elif not rescan_ok:
            # 複查自己也掛了 → 「沒有倖存者」這個結論沒有根據。不要沉默地
            # 讓使用者以為清乾淨了。
            lines.append("⚠️ 清完後的複查沒跑完，不確定是不是真的都關掉了")
    # **這一段刻意放在 `if chrome_procs:` 外面。** 掃描一開始就炸的時候名單是
    # 空的，上面整段會被跳過——而那正是最需要說話的情況：使用者看到的畫面會跟
    # 「機器上本來就很乾淨」一模一樣，然後下一次啟動瀏覽器才撞上殘留的 lock。
    if not scan_ok:
        print("terminate: chrome scan incomplete; sweep may have missed "
              "processes", file=sys.stderr)
        lines.append(
            "⚠️ 瀏覽器行程清單沒有掃完整，可能還有殘留沒清掉；"
            "若接下來啟動失敗，請再執行一次。")
    # 同樣的理由，同樣刻意放在 `if survivors:` 外面：掃不成的時候名單是空的，
    # 而空名單跟「真的沒有漏網的」在畫面上一模一樣。這一句的後果比 chrome 那句
    # 更重——漏掉的是**背景產圖程式本體**，它會自己再開一個瀏覽器，兩個實例同時
    # 寫同一份佇列與同一個瀏覽器設定檔。
    if not wr_scan_ok:
        print("terminate: background-process scan incomplete; sweep may have "
              "missed instances", file=sys.stderr)
        lines.append(
            "⚠️ 背景程式清單沒有掃完整，可能還有實例沒清掉；"
            "請用 `/sys health` 再確認一次。")
    return lines


# ---------- 獨立監督者：脫離行程樹的啟動 ＋ 探查 ＋ 終止 --------------------
#
# 這一段服務的是「從對話平台啟動**獨立**監督者」那條路。獨立的價值只有一個：
# bot 掛掉／重啟時它還活著，繼續監督批次。所以「真的脫離」是這條路唯一的功能
# 需求——做不到的話，它跟 bot 內建的那套監督者一模一樣，而使用者會被告知
# 「獨立監督者已啟動」。那正是本專案最不能接受的失敗形態：回覆說成功、實際上
# 沒有達成目的、而且沒有任何症狀。
#
# **實測（2026-09-12，本機 Windows 11 / CPython 3.14.4）四種做法的結果**——
# 情境是「起一個目標行程，然後把上游那條 bot 鏈殺掉」：
#
# | 做法 | 直接殺掉 bot 那個行程 | `taskkill /F /T` 整棵樹 |
# |---|---|---|
# | 一般 `Popen` | 目標**活著** | 目標**死掉** |
# | `DETACHED_PROCESS`＋`CREATE_NEW_PROCESS_GROUP` | 活著 | **死掉** |
# | 再加 `CREATE_BREAKAWAY_FROM_JOB` | 活著 | **死掉** |
# | **經由會立刻結束的中繼行程**（本函式） | 活著 | **活著** |
#
# 兩個容易搞錯的前提，先講清楚：
#
# 1. **殺不掉的不是 job object。** 本機每一個 Python 行程的 immediate job 旗標
#    都是 `0x3000`＝`SILENT_BREAKAWAY_OK | KILL_ON_JOB_CLOSE`。`SILENT_BREAKAWAY_OK` 的語意就是「這個
#    job 裡的行程生出來的子行程**不會**被放進這個 job」，所以 bot 的子行程從一
#    開始就在 bot 的 job 外面——bot 死掉並不會透過 job 帶走它。上表第一欄全部
#    「活著」就是這件事的實測版。
# 2. **真正會帶走它的是「父子關係」。** `taskkill /F /T` 是照 `ParentProcessId`
#    往下走的，跟 job 無關。所以 `CREATE_BREAKAWAY_FROM_JOB` 幫不上忙（實測那
#    一列就是證據，不是推論）——它解的是 job，不是行程樹。
#
# 於是唯一有效的手段是**讓父子關係斷掉**：先起一個中繼行程，由它去起真正的目標，
# 然後中繼行程立刻結束。目標的 `ParentProcessId` 從此指向一個已經不存在的 pid，
# 上游那棵樹怎麼走都走不到它。
#
# **中繼刻意不走 shell。** `cmd /c start "" /B …` 是 Windows 上更常見的寫法，實測
# 也有效，但它把所有參數丟進命令列剖析器：路徑裡一個 `&` 就會被當成指令分隔，而
# 這條路徑上的字串是從 `__file__` 推出來的（clone 到哪裡由使用者決定）。本專案
# 其餘的 spawn 一律標著「不經 shell」，這裡沒有理由破例——中繼是本模組自己，參數
# 以 argv 清單傳遞，引號與 metacharacter 的整個問題類別都不存在。
DETACH_RELAY_FLAG = "--detach-relay"

# 獨立監督者的腳本檔名。探查與終止都以「命令列裡某個參數就是這支腳本」為準，
# 不是子字串比對——理由與 `_find_all_webrunner_pids` 相同，下游是 `taskkill /F`。
LAUNCHER_SCRIPT_NAME = "start_webrunner.py"

if os.name == "nt":                                     # pragma: no cover
    _DETACH_FLAGS = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
else:
    _DETACH_FLAGS = 0


def _detach_popen(argv: list[str], cwd: str) -> None:
    """起一個不綁主控台、不共用任何 handle 的行程。三個 stdio 都導到空裝置。

    導到空裝置是刻意的：目標（獨立監督者）自己會把每一行寫進它的記錄檔，讓它
    的 stdout 再落地一次只會變成同一份記錄的兩份副本。空裝置也保證它不會因為
    「沒有人讀管線」而卡死——那正是 `_supervisor.pump_stream` 存在的理由，而中
    繼行程不可能留下來抽水。
    """
    subprocess.Popen(  # nosec B603 - argv 由呼叫端組出來，不經 shell  # pylint: disable=consider-using-with
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_DETACH_FLAGS,
    )


def spawn_detached(argv: list[str], *, cwd: str) -> bool:
    """把 `argv` 起成一個**脫離呼叫者行程樹**的行程。回「中繼有沒有起得來」。

    回傳值只說「中繼行程生出來了」，**不代表目標真的在跑**——依定義我們拿不到
    目標的 handle（那正是脫離的意思）。呼叫端必須另外去**觀測**目標是否出現，
    不可以拿這個 True 當成「已啟動」回報給使用者。
    """
    relay = [sys.executable, str(Path(__file__).resolve()),
             DETACH_RELAY_FLAG, cwd, *argv]
    try:
        _detach_popen(relay, cwd)
        return True
    except OSError as error:
        # `!r` 而不是 `{error}`：`OSError` 的 str 會帶出檔案路徑。
        print(f"spawn_detached failed: {error!r}", file=sys.stderr)
        return False


def _relay_main(raw: list[str]) -> int:
    """中繼行程的進入點：把真正的目標起起來，然後**立刻結束**。

    結束得越快越好——中繼還活著的那段時間裡，目標仍然掛在呼叫者的行程樹下面，
    這條路要防的 `taskkill /F /T` 在那個視窗裡仍然打得到它。
    """
    if len(raw) < 2:
        return 2
    cwd, argv = raw[0], list(raw[1:])
    try:
        _detach_popen(argv, cwd)
    except OSError as error:
        print(f"detach relay failed: {error!r}", file=sys.stderr)
        return 1
    return 0


def find_launcher_pids(
        script_name: str = LAUNCHER_SCRIPT_NAME) -> tuple[list[int], bool]:
    """找正在執行的獨立監督者。回 `(pid 清單, 掃描是否完整)`。

    **第二個值不是裝飾品。** `_supervisor.other_launcher_pids` 也做類似的事，但
    它的合約明寫「永遠不參與決策、psutil 不在就回空 list」——拿那個來判斷「有沒有
    在跑」會把「掃不成」讀成「沒有」，然後在一個已經有監督者的機器上再開一個。
    本函式因此另外寫一份：同樣的命中條件，但把「不知道」講出來。

    回傳的是 `collapse_interpreter_stub_pairs` 併過的**最外層** pid（見該函式）；
    終止它會連轉接殼底下的本尊一起帶走。
    """
    raw: list[tuple[int, int, str]] = []
    try:
        import psutil  # type: ignore  # noqa: PLC0415
    except ImportError:
        return [], False
    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if pid == own_pid:
                    continue
                if not looks_like_python_process(proc.info.get("name")):
                    continue
                if cmdline_runs_script(proc.cmdline(), (script_name,)):
                    raw.append((pid, proc.ppid() or 0, script_name))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 列舉途中行程消失／取不到權限是常態，不算掃描失敗。
                continue
    except Exception as error:  # pylint: disable=broad-except
        print(f"find_launcher_pids failed: {error!r}", file=sys.stderr)
        return [pid for pid, _script in collapse_interpreter_stub_pairs(raw)], False
    return [pid for pid, _script in collapse_interpreter_stub_pairs(raw)], True


def terminate_launcher_pids(pids: list[int]) -> int:
    """強制終止這些獨立監督者，回實際動手的筆數。

    **刻意不帶 `/T`。** 監督者底下掛著正在產圖的背景程式與整棵瀏覽器，用樹狀終止
    會把它們一起硬砍掉；`/stop` 既有的清理流程本來就會用「先客氣、5 秒後強制」的
    方式處理那一半，這裡只負責把「會再生一個」的那層拿掉，其餘維持原本的行為。
    """
    killed = 0
    for pid in pids:
        if not _pid_alive(pid):
            continue
        _force_kill_pid(pid)
        killed += 1
    return killed


if __name__ == "__main__":          # pragma: no cover - 中繼行程的進入點
    if len(sys.argv) > 1 and sys.argv[1] == DETACH_RELAY_FLAG:
        raise SystemExit(_relay_main(sys.argv[2:]))
    raise SystemExit(2)
