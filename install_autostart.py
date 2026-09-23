"""登入時自動把每個開著的平台與批次監督者拉起來（Windows 工作排程器）。

**為什麼需要這個。** 監督者能撐住「子行程掛掉」，撐不住「整台機器掛掉」——而長時間
無人值守的主機就是會重開：顯示驅動 bugcheck、Windows Update、斷電。量過一次真實的
代價：主機在晚上重開，但沒有任何東西會把 bot 與批次拉回來，直到隔天有人手動開，
**整套停了約 23 小時**。無人值守的批次因此白等了一整天，而從外面看不出任何異狀
——沒有崩潰訊息，就只是「沒有在跑」。

裝上這個之後，同一次重開的代價變成「開機 ＋ 登入」那幾十秒。

**為什麼是「登入時」而不是「開機時」。** 批次要開一個真的看得見的 Chrome
（webrunner 起來之後把視窗縮到最小，但那仍然是一個有桌面的視窗）。開機觸發的工作
跑在 session 0、沒有互動桌面，Chrome 在那裡起不來。所以觸發條件是**目前這個使用者
登入時**，用 `InteractiveToken` 跑在他自己的桌面 session 裡。代價講清楚：機器重開
後停在鎖定畫面、沒有人登入的話，這個工作不會觸發。要真正做到「重開機就自動繼續」，
還需要在系統層開自動登入——那是主機安全設定，不在這支腳本的範圍內，也不該由它
偷偷改掉。

**重複啟動由誰擋。** 每一支啟動器各自持有一把單一實例鎖（bot 那一把是**逐平台**
的，住在 `state/<平台>/`；批次那一把是 `.webrunner_supervisor.lock`），所以「使用者
自己已經開著、登入工作又開一個」會被乾淨地擋掉並寫進 log。批次那一把就是為了這條路
才補上的——在那之前，自動啟動等於把偶發的雙開變成常態。

**註冊的是「現在開著的東西」，不是一對寫死的工作。** 一個平台一個行程、一個行程
一筆排程工作（`\\Axiomatic\\Bot-<平台>`），所以 `--install` 會去讀 `bot_config.json`
算出哪些平台開著而且填了憑證，替每一個各註冊一筆。**沒開或沒填憑證的平台不會有
工作**——那是「缺席」，不是「失敗」。反過來，`--remove` 掃的是排程器裡**整個
`\\Axiomatic\\` 資料夾**，不是同一份計算結果：一個剛被關掉的平台，它的工作仍然
留在排程器裡，而用「現在開著哪些」去算要刪誰的話，那筆工作會被永遠遺留下來，
每次登入照常把一個已經關掉的平台拉起來。

用法：
    py -3 install_autostart.py --install     # 註冊（可重複執行，冪等）
    py -3 install_autostart.py --status      # 看目前註冊了什麼
    py -3 install_autostart.py --remove      # 移除
    py -3 install_autostart.py --install --bot-only
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess  # nosec B404 — 這支腳本的工作就是呼叫 schtasks
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from axiomatic import _platform_runtime
from axiomatic._bot_config import load_bot_config

REPO_ROOT = Path(__file__).resolve().parent

# 工作名稱。放在 `\Axiomatic\` 資料夾底下，`--remove` 才好整組收掉，也不會跟
# 使用者其他的排程混在一起。**名字本身由 `_platform_runtime` 算**：`/sys doctor`
# 的自動復原檢查用的是同一份計算，兩份手抄的名單只要改一邊不改另一邊，症狀就是零。
TASK_FOLDER = _platform_runtime.AUTOSTART_TASK_FOLDER
BATCH_TASK = (_platform_runtime.AUTOSTART_BATCH_TASK, "start_webrunner.py", [])


def bot_task(platform: str) -> tuple[str, str, list[str]]:
    """一個平台一筆工作：`\\Axiomatic\\Bot-<平台>` → `start_discord_bot.py --platform`。"""
    return (_platform_runtime.autostart_bot_task(platform),
            "start_discord_bot.py", ["--platform", platform])


def planned_tasks(which: list[str]) -> list[tuple[str, str, list[str]]]:
    """`--install` 這一輪要註冊的工作。開著的平台各一筆，批次一筆。

    名稱那一半刻意繞回 `_platform_runtime.autostart_task_names()`（而不是自己把
    平台清單再跑一次），所以「註冊了什麼」與「doctor 查什麼」在程式碼上就是同一
    個答案，不必靠一支測試去對帳兩份名單。
    """
    tasks: list[tuple[str, str, list[str]]] = []
    if "bot" in which:
        config = load_bot_config()
        tasks += [bot_task(name)
                  for name in _platform_runtime.enabled_platforms(config)]
    if "batch" in which:
        tasks.append(BATCH_TASK)
    return tasks


def python_command() -> list[str]:
    """**刻意偏離 DoD #5**：`.venv/Scripts/` → 目前的直譯器，中間**沒有** `py -3`。

    DoD #5 那套三步探索（`.venv` → `py -3` → `sys.executable`）是給**兩支啟動器**
    的，它假設有個人站在主控台前面、看得到錯誤訊息。這支腳本把命令寫進工作排程器，
    跑的時候沒有人在看，判準因此不同：**寫進去的命令必須是絕對路徑，而且不會在執行
    期重新解析**。`py -3` 兩個條件都不滿足——`py.exe` 是轉接器，`-3` 要到執行當下才
    去查登錄檔的 `PythonCore`、`PY_PYTHON` / `PY_PYTHON3` 環境變數、`%LOCALAPPDATA%`
    底下的 `py.ini` 與腳本的 shebang，而登入工作拿到的環境跟互動 shell 不一樣。於是
    它**可能**解析到另一個直譯器，而且**一定不會**解析到 `.venv`（虛擬環境不會註冊
    給 py 轉接器）——正式行程換了一組相依套件在跑，而症狀要到下一次重新開機才出現，
    現場沒有人在。寫死絕對路徑則不會漂。

    保底那一步是 `sys.executable`，也就是**執行 `--install` 的那個直譯器**：安裝當下
    已知可用、是絕對路徑，而且 `--status` 的「執行工作」欄位會把它印出來，人看得到
    到底綁了哪一個。

    只找 `.venv/Scripts/`、不找 `.venv/bin/` 同樣是刻意的，不是漏掉：`main()` 在
    非 Windows 上就直接收工，這支腳本只服務 Windows 工作排程器。

    ⚠️ 改這裡之前先讀 `test/test_supervisor.py` 的
    `test_the_autostart_copy_is_deliberately_different_not_a_missed_one`——把 `py -3`
    加回來會讓它變紅，那**不是**誤報。

    **也刻意不用 `pythonw.exe`。** 沒有主控台看起來比較乾淨（也就沒有一個可以被
    誤關的視窗，那是踩過的坑），但排程器啟動的 `pythonw` 沒有繼承任何
    標準控制代碼，`sys.stdout` 會是 `None`——而兩支啟動器與底下的 bot／webrunner
    到處都在 `print()`，第一行就會炸成 `AttributeError: 'NoneType' object has no
    attribute 'write'`。用 `python.exe` 換到的是一個會出現的主控台視窗：關掉它會
    停掉那一套（記錄檔照樣留著），但至少它跑得起來。
    """
    for name in ("python.exe", "python"):
        venv_py = REPO_ROOT / ".venv" / "Scripts" / name
        if venv_py.exists():
            return [str(venv_py)]
    return [sys.executable]


def _task_xml(script: str, user: str, extra: list[str] | None = None) -> str:
    """工作排程器的 XML 定義。

    每一項設定都是刻意的，改之前先讀完：

    * `ExecutionTimeLimit` = `PT0S`（無限）。**預設是 72 小時**，時間到排程器會
      直接把工作砍掉——對一個要跑好幾天的批次來說，那是一顆定時炸彈，而且症狀
      會長得跟「又崩潰了」一模一樣。
    * `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries` 都關掉。預設是開的，
      筆電拔掉電源就會把批次停掉。
    * `MultipleInstances` = `IgnoreNew`：排程器自己也不要開出第二份（啟動器的鎖是
      第二道防線，不是唯一那道）。
    * `RestartOnFailure`：監督者本身如果整支掛了（不是它的子行程掛了），隔一分鐘
      再試，最多三次。
    * `LogonType` = `InteractiveToken`：跑在使用者自己的桌面 session，Chrome 才
      開得起來。
    * `RunLevel` = `LeastPrivilege`：不需要系統管理員權限，也不該要。
    """
    cmd = python_command()
    exe = cmd[0]
    args = " ".join(cmd[1:] + [f'"{REPO_ROOT / script}"']
                    + list(extra or [])).strip()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(script)} — 登入時自動啟動（見 install_autostart.py）</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(REPO_ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    """`encoding="oem"`，**不是** `"utf-8"`。

    CLAUDE.md 的「文字 I/O 一定要指名編碼」這條，在 Windows 主控台工具上還有第二
    層：指名了還得指對。`schtasks` 是主控台程式，輸出走的是 **OEM 代碼頁**
    （繁體中文 Windows 上是 cp950），不是 UTF-8。餵 `encoding="utf-8",
    errors="replace"` 不會丟例外——它會**安靜地**把每個中文欄位名變成替換字元，
    於是 `--status` 的欄位比對一條都對不上，印出一片空白，看起來像「工作沒註冊」。

    `"oem"` 是 Python 在 Windows 上對 `GetOEMCP()` 的別名，所以它跟著機器的地區
    設定走，不必寫死 cp950。`errors="replace"` 保留：這是外部行程的輸出。
    """
    return subprocess.run(  # nosec B603 B607 — 固定命令、參數不來自外部輸入
        ["schtasks", *args],
        capture_output=True, text=True,
        encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
        check=False,
    )


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def install(which: list[str]) -> int:
    user = _current_user()
    if not user:
        print("找不到目前的使用者名稱（USERNAME 沒設定）；無法註冊。",
              file=sys.stderr)
        return 1
    tasks = planned_tasks(which)
    if not tasks:
        # 一個都算不出來的時候要出聲。「註冊完成、但一筆工作都沒有」跟「註冊成功」
        # 印起來一模一樣，而症狀要到下一次重開機才出現。
        print("沒有任何東西要註冊：平台全都關著或沒填憑證。", file=sys.stderr)
        print("跑 `py -3 start_platforms.py --list` 看逐平台的原因。",
              file=sys.stderr)
        return 1
    rc = 0
    for task_name, script, extra in tasks:
        if not (REPO_ROOT / script).exists():
            print(f"找不到 {script}，跳過 {task_name}", file=sys.stderr)
            rc = 1
            continue
        # XML 必須是 UTF-16 且帶 BOM——schtasks /XML 只吃這個，餵 UTF-8 會回一句
        # 「載入工作 XML 失敗」而不會說是編碼問題。
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
            tmp = handle.name
        try:
            Path(tmp).write_text(_task_xml(script, user, extra),
                                 encoding="utf-16")
            # `/F` ＝ 已存在就覆寫，讓這支腳本可以重複執行。
            done = _schtasks("/Create", "/TN", task_name, "/XML", tmp, "/F")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if done.returncode == 0:
            print(f"已註冊：{task_name} → {script} {' '.join(extra)}".rstrip())
        else:
            print(f"註冊 {task_name} 失敗：{done.stdout.strip()} "
                  f"{done.stderr.strip()}", file=sys.stderr)
            rc = 1
    return rc


def registered_tasks() -> list[str]:
    """排程器裡 `\\Axiomatic\\` 底下現在真的有哪些工作。

    **`--remove` 與 `--status` 問的是排程器，不是設定檔。** 用「現在開著哪些平台」
    去算要刪誰的話，一個剛被關掉的平台的工作會永遠留在排程器裡，每次登入照常把它
    拉起來——而 `--status` 也看不到它，因為那份計算結果裡沒有它。這正是本 repo
    一再記錄的「兩份平行清單」形狀，差別在這一份的正本在作業系統那邊。
    """
    done = _schtasks("/Query", "/FO", "LIST")
    if done.returncode != 0:
        return []
    found: list[str] = []
    prefix = TASK_FOLDER + chr(92)
    for line in done.stdout.splitlines():
        _field, _sep, value = line.partition(":")
        name = value.strip()
        if name.startswith(prefix) and name not in found:
            found.append(name)
    return sorted(found)


def _wanted_task_names(which: list[str]) -> list[str]:
    """`--remove` / `--status` 要處理哪些**已註冊**的工作名。"""
    names = registered_tasks()
    bot_prefix = TASK_FOLDER + chr(92) + "Bot"
    batch_name = BATCH_TASK[0]
    out = []
    for name in names:
        is_batch = name == batch_name
        if is_batch and "batch" in which:
            out.append(name)
        elif not is_batch and name.startswith(bot_prefix) and "bot" in which:
            out.append(name)
    return out


def remove(which: list[str]) -> int:
    rc = 0
    targets = _wanted_task_names(which)
    if not targets:
        print("（`\\Axiomatic\\` 底下沒有符合的工作，本來就不存在）")
        return 0
    for task_name in targets:
        done = _schtasks("/Delete", "/TN", task_name, "/F")
        if done.returncode == 0:
            print(f"已移除：{task_name}")
        elif "ERROR: The system cannot find" in done.stderr or "找不到" in (
                done.stderr + done.stdout):
            print(f"（{task_name} 本來就不存在）")
        else:
            print(f"移除 {task_name} 失敗：{done.stdout.strip()} "
                  f"{done.stderr.strip()}", file=sys.stderr)
            rc = 1
    return rc


# ---------------------------------------------------------------------------
# 「上次結果」的解碼
# ---------------------------------------------------------------------------
# `schtasks /V` 把「上次結果」印成一個裸的十進位整數，例如 `267009` 或
# `-2147020576`。那個數字對讀的人來說是零資訊——而這支工具存在的唯一理由就是回答
# 「復原機制現在是不是活的、上一次有沒有成功」。實測：批次那一筆是 `-2147020576`，
# 看起來像出事了，解出來是 `0x800710E0`「操作員或系統管理員已拒絕此要求」，也就是
# **有人手動停掉它**，完全正常。反過來說，真正該緊張的
# `0x8004130F`（帳戶資訊沒設定 → 工作註冊得起來但永遠不會觸發）長得一模一樣，
# 也是一串負數。**分不出這兩者的狀態輸出，等於沒有狀態輸出。**
#
# 訊息文字**向作業系統要**，不自己維護一張表：`FormatMessageW` 連
# `SCHED_S_*`／`SCHED_E_*` 都解得出來（實測 12 個碼全中），而且會跟著系統語言走。
# 自己抄一份就是 CLAUDE.md 那條「兩份平行清單，總有一份會被遺忘」的翻版，何況
# 抄錯的後果正是這裡要修的毛病——一個講得頭頭是道但錯的解釋。

# HRESULT 的高 16 位元。`0x0004`＝SCHED_S_*（成功類狀態），`0x8004`＝SCHED_E_*，
# `0x8007`＝FACILITY_WIN32 包起來的一般系統錯誤。
_HIGH_WORD_LABELS = {
    0x0004: "排程器狀態",
    0x8004: "排程器錯誤",
    0x8007: "系統錯誤",
}


def _parse_last_result(text: str) -> int | None:
    """把「上次結果」的值解析成 32 位元不帶號整數；解不出來回 `None`。

    `schtasks` 印十進位（負數代表高位元是 1 的 HRESULT），但別的地區設定或
    別的版本印過十六進位，所以兩種都收。
    """
    token = text.strip().split()[0] if text.strip() else ""
    try:
        value = int(token, 16) if token.lower().startswith(("0x", "-0x")) \
            else int(token)
    except ValueError:
        return None
    return value & 0xFFFFFFFF


def _system_message(code: int) -> str:
    """向作業系統要 `code` 的說明文字。拿不到就回空字串。

    只有 Windows 有 `FormatMessageW`；拿不到訊息**不是**錯誤，狀態輸出照印，
    只是少一句解釋。這支工具的職責是報告，不是在報告的路上自己炸掉。
    """
    if os.name != "nt":
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(2048)
        written = kernel32.FormatMessageW(
            0x00001000 | 0x00000200,   # FROM_SYSTEM | IGNORE_INSERTS
            None, ctypes.c_uint32(code), 0, buffer, len(buffer), None)
    except (OSError, AttributeError, ValueError):
        return ""
    return buffer.value.strip() if written else ""


def _explain_last_result(text: str) -> str:
    """`--status` 印在「上次結果」後面的那句話；沒話可說就回空字串。"""
    value = _parse_last_result(text)
    if value is None:
        return ""
    if value == 0:
        return "← 成功"
    label = _HIGH_WORD_LABELS.get(value >> 16)
    if label is None:
        if value & 0x80000000:
            label = "HRESULT"
        else:
            # **刻意不解碼。** `FormatMessageW` 對小整數會回 Win32 錯誤訊息，於是
            # 啟動器自己 `sys.exit(2)` 會被講成「系統找不到指定的檔案」——一個聽
            # 起來很合理、但完全錯的解釋。寧可少說一句，也不要誤導。
            return f"← 這是被啟動的程式自己的結束碼（{value}），不是排程器的碼"
    message = _system_message(value)
    detail = f"，{message}" if message else ""
    return f"← 0x{value:08X}（{label}{detail}）"


def status(which: list[str]) -> int:
    targets = _wanted_task_names(which)
    if not targets:
        print("`\\Axiomatic\\` 底下沒有註冊任何工作。")
        print("要註冊：py -3 install_autostart.py --install")
        return 0
    for task_name in targets:
        done = _schtasks("/Query", "/TN", task_name, "/V", "/FO", "LIST")
        if done.returncode != 0:
            print(f"{task_name}: 未註冊")
            continue
        # 中英兩套欄位名都列：repo 不該綁死在一個地區設定。
        # 「如果執行 X 小時…就停止工作」要顯示出來——那是排程器預設 72 小時砍掉
        # 工作的那個設定，對跑好幾天的批次是致命的，值得每次 status 都看一眼。
        wanted = ("工作名稱", "TaskName", "狀態", "Status", "下次執行時間",
                  "Next Run Time", "上次執行時間", "Last Run Time",
                  "上次結果", "Last Result", "執行工作", "Task To Run",
                  "開始位置", "Start In", "排程類型", "Schedule Type",
                  "登入模式", "Logon Mode",
                  "如果執行", "Stop Task If Runs")
        for line in done.stdout.splitlines():
            if not any(line.strip().startswith(w) for w in wanted):
                continue
            tidy = " ".join(line.split())
            if line.strip().startswith(("上次結果", "Last Result")):
                _field, _sep, value = tidy.partition(":")
                hint = _explain_last_result(value)
                if hint:
                    tidy = f"{tidy}  {hint}"
            print("  " + tidy)
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="登入時自動啟動每個開著的平台與批次監督者"
                    "（Windows 工作排程器）。")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true", help="註冊（冪等）")
    action.add_argument("--remove", action="store_true", help="移除")
    action.add_argument("--status", action="store_true", help="顯示目前狀態")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--bot-only", action="store_true")
    scope.add_argument("--batch-only", action="store_true")
    args = parser.parse_args()

    if os.name != "nt":
        print("這支腳本只支援 Windows 工作排程器。", file=sys.stderr)
        return 1

    which = (["bot"] if args.bot_only
             else ["batch"] if args.batch_only
             else ["bot", "batch"])
    if args.install:
        return install(which)
    if args.remove:
        return remove(which)
    return status(which)


if __name__ == "__main__":
    sys.exit(main())
