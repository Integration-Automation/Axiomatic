"""`install_autostart.py` 的守門測試。

**為什麼這支值得存在。** 這個功能的失效形態全部是安靜的：工作註冊成功、狀態顯示
「就緒」、然後在某個沒人看著的時刻沒有照預期跑，或是跑了但被排程器自己砍掉。沒有
一條會在註冊當下報錯。

背景：主機曾經在深夜未正常關機重開
（系統事件記錄裡這是第 14 次），而沒有任何東西會把 bot 與批次拉回來——整套停了約
23 小時，直到有人手動開。監督者撐得住「子行程掛掉」，撐不住「整台機器掛掉」。

這支測試不去碰真的工作排程器（那需要改主機狀態），只驗**產生出來的 XML 定義**與
CLI 的形狀。真正的端到端驗證是手動做的：註冊後 `schtasks /Run` 觸發，確認啟動器
被單一實例鎖乾淨擋掉並寫進 log（2026-09-03 兩支都實測過）。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install_autostart as ia  # noqa: E402

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture(scope="module")
def xml_root():
    return ElementTree.fromstring(ia._task_xml("start_webrunner.py", "HOST\\USER"))


def _text(root, path):
    node = root.find(path, _NS)
    return None if node is None else (node.text or "")


def test_the_execution_time_limit_is_unlimited(xml_root):
    """**這一條是整支測試最重要的。**

    工作排程器的 `ExecutionTimeLimit` 預設是 **72 小時**，時間到就直接把工作砍掉。
    對一個要連續跑好幾天的批次來說那是定時炸彈，而且症狀跟「又崩潰了」一模一樣：
    行程消失、log 斷在半路、沒有任何錯誤訊息。`PT0S` 才是「不限制」。
    """
    assert _text(xml_root, "t:Settings/t:ExecutionTimeLimit") == "PT0S", (
        "ExecutionTimeLimit 不是 PT0S（無限）——排程器會在預設 72 小時後把批次砍掉。")


def test_battery_settings_do_not_stop_the_batch(xml_root):
    """兩個都預設為 true，會讓筆電一拔電源就停掉批次。"""
    assert _text(xml_root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert _text(xml_root, "t:Settings/t:StopIfGoingOnBatteries") == "false"


def test_the_task_runs_in_an_interactive_desktop_session(xml_root):
    """批次要開一個真的 Chrome。開機觸發／SYSTEM 帳號跑在 session 0，沒有互動
    桌面，Chrome 在那裡起不來——所以必須是登入觸發 ＋ InteractiveToken。"""
    assert _text(xml_root, "t:Principals/t:Principal/t:LogonType") == \
        "InteractiveToken"
    assert xml_root.find("t:Triggers/t:LogonTrigger", _NS) is not None, (
        "不是登入觸發——開機觸發沒有互動桌面，Chrome 起不來。")


def test_the_scheduler_does_not_start_a_second_copy(xml_root):
    """啟動器的單一實例鎖是第二道防線，不是唯一那道。"""
    assert _text(xml_root, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"


def test_it_does_not_ask_for_administrator(xml_root):
    """不需要就不要要——要了之後每次登入都可能跳 UAC，而沒人會去按。"""
    assert _text(xml_root, "t:Principals/t:Principal/t:RunLevel") == \
        "LeastPrivilege"


def test_the_working_directory_is_the_repo_root(xml_root):
    assert _text(xml_root, "t:Actions/t:Exec/t:WorkingDirectory") == str(REPO_ROOT)


def test_the_command_is_a_real_interpreter_and_the_script_is_absolute(xml_root):
    command = _text(xml_root, "t:Actions/t:Exec/t:Command")
    args = _text(xml_root, "t:Actions/t:Exec/t:Arguments")
    assert command and Path(command).is_absolute(), (
        f"直譯器路徑不是絕對路徑：{command!r}。排程器的 PATH 跟互動 shell 不同。")
    assert "start_webrunner.py" in args and str(REPO_ROOT) in args


def test_it_does_not_use_pythonw(xml_root):
    """`pythonw.exe` 沒有繼承標準控制代碼，`sys.stdout` 會是 None，而啟動器與
    底下的 bot／webrunner 到處都在 `print()`——第一行就炸。"""
    command = _text(xml_root, "t:Actions/t:Exec/t:Command")
    assert "pythonw" not in command.lower(), (
        "用了 pythonw：排程器啟動時沒有主控台，`print()` 會丟 AttributeError。")


def test_the_interpreter_prefers_the_local_venv():
    """與兩支啟動器同一條 DoD #5 規則：`.venv` 優先，正式行程才不會悄悄換直譯器。"""
    got = ia.python_command()[0]
    if (REPO_ROOT / ".venv" / "Scripts" / "python.exe").exists():
        assert ".venv" in got, f"沒有優先用 .venv：{got}"


def test_every_task_name_lives_under_one_folder():
    """全部放在 `\\Axiomatic\\` 底下，`--remove` 才好整組收掉，也不會跟使用者
    自己的排程混在一起。"""
    for task_name, script in ia.TASKS.values():
        assert task_name.startswith(ia.TASK_FOLDER + "\\"), task_name
        assert (REPO_ROOT / script).exists(), f"{script} 不存在"


def _unchecked_and_unregistered(registered, checked):
    """兩個方向各回一份：註冊了但沒人查的、查了但沒人註冊的。

    抽成純函式是為了讓它能有**自己的**對照組。兩邊一致的時候，把下面那支主測試
    的斷言整個刪掉本來就不會有人紅，所以牙齒要長在
    `test_the_autostart_reconciler_sees_both_directions` 那支合成資料上。
    """
    return (sorted(set(registered) - set(checked)),
            sorted(set(checked) - set(registered)))


def test_the_doctor_checks_exactly_the_tasks_that_get_registered():
    """`install_autostart` 註冊的工作名稱，要跟 `/sys doctor` 查的那份完全一致。

    這兩份名稱是**各自手抄**的：`install_autostart.TASKS` 用
    `rf"{TASK_FOLDER}\\Bot"` 組出來，`_process_control._AUTOSTART_TASKS` 則是一個
    寫死的 tuple。中間沒有共用常數，也沒有任何東西比對過。

    改一邊不改另一邊的症狀是**零**：`schtasks /Query` 對一個不存在的名稱只是回
    非 0，`autostart_recovery_status` 把它算進 `missing`，於是 `/sys doctor` 從此
    每次都說「自動復原鏈路缺一角」——一個永遠為真的警告，看的人第三次就會忽略它。
    反過來（名單漏一筆）更糟：那支工作真的沒註冊也不會有人說話，而這整套機制存在
    的理由就是 2026-09-02 那次主機重開後停擺 23 小時。

    **不查真的工作排程器。** 那會讓測試依賴這台機器當下的狀態；這裡只對帳兩份
    原始碼裡的名單。真的有沒有註冊是 `--status` 的事。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))
    import _process_control as pc      # noqa: E402  (延遲匯入，見上一行)

    registered = {name for name, _script in ia.TASKS.values()}
    checked = set(pc._AUTOSTART_TASKS)
    # 正面對照組要兩邊都做：任一邊空掉，下面那句差集會是空的、斷言永遠通過。
    assert len(registered) >= 2, "`install_autostart.TASKS` 空了或只剩一筆"
    assert len(checked) >= 2, "`_AUTOSTART_TASKS` 空了或只剩一筆"

    unchecked, unregistered = _unchecked_and_unregistered(registered, checked)
    assert not unchecked, (
        "這些工作 `install_autostart` 會註冊，但 `/sys doctor` 不會查：%s。"
        "沒註冊成功也不會有人說話。" % unchecked)
    assert not unregistered, (
        "這些工作 `/sys doctor` 會查，但沒有任何東西會註冊它們：%s。"
        "`/sys doctor` 會從此永遠回報「缺一角」。" % unregistered)


def test_the_autostart_reconciler_sees_both_directions():
    """對帳器自己的正面對照組：拿合成資料確認兩個方向都看得見。

    需要這一支的理由跟 `test_bot_helpers._stale_lock_entries` 一樣：現況兩邊一致，
    所以上面那支主測試把斷言刪掉也不會紅。
    """
    same = {"\\A\\Bot", "\\A\\Batch"}
    assert _unchecked_and_unregistered(same, set(same)) == ([], [])
    # 註冊了但沒人查
    assert _unchecked_and_unregistered(same, {"\\A\\Bot"}) == (["\\A\\Batch"], [])
    # 查了但沒人註冊（例如工作被改名，doctor 那份留著舊名）
    assert _unchecked_and_unregistered({"\\A\\Bot"}, same) == ([], ["\\A\\Batch"])
    # 兩邊都有各自的落單
    assert _unchecked_and_unregistered({"\\A\\Bot"}, {"\\A\\Batch"}) == (
        ["\\A\\Bot"], ["\\A\\Batch"])


def test_the_xml_escapes_a_hostile_user_name():
    """使用者名稱來自環境變數，直接內插進 XML 會壞掉（`&` 是合法的 Windows
    帳號字元）。壞掉的 XML 只會讓 schtasks 回一句「載入失敗」。"""
    xml = ia._task_xml("start_webrunner.py", "DOM\\a&b<c>")
    root = ElementTree.fromstring(xml)          # 解析不過就直接炸在這裡
    assert _text(root, "t:Principals/t:Principal/t:UserId") == "DOM\\a&b<c>"


def test_schtasks_output_is_not_decoded_as_utf8():
    """`schtasks` 是主控台程式，輸出走 OEM 代碼頁。用 utf-8 解不會丟例外——它會
    **安靜地**把中文欄位名變成替換字元，於是 `--status` 一個欄位都對不上、印出
    一片空白，看起來就像「工作沒註冊」。2026-09-03 實際踩到。

    用 AST 檢查而不是字串比對：上面這段註解本身就寫了 `utf-8`。
    """
    tree = ast.parse(Path(ia.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_schtasks")
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run")
    enc = next((kw.value for kw in call.keywords if kw.arg == "encoding"), None)
    assert enc is not None, "_schtasks 沒有指名 encoding"
    literals = {n.value for n in ast.walk(enc)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "oem" in literals, (
        f"_schtasks 的 encoding 沒有 'oem'：{literals}。Windows 主控台工具的輸出"
        "走 OEM 代碼頁，指名 utf-8 會安靜地解成亂碼。")


@pytest.mark.parametrize("argv,expect", [
    (["--install"], ["bot", "batch"]),
    (["--install", "--bot-only"], ["bot"]),
    (["--install", "--batch-only"], ["batch"]),
])
def test_the_scope_flags_select_the_right_tasks(argv, expect, monkeypatch):
    """`--install` 不帶範圍旗標時**兩個都裝**，這是預設也是最常用的路徑。"""
    seen = []
    monkeypatch.setattr(ia, "install", lambda which: seen.append(which) or 0)
    monkeypatch.setattr(sys, "argv", ["install_autostart.py", *argv])
    monkeypatch.setattr(os, "name", "nt")
    assert ia.main() == 0
    assert seen == [expect]


def test_an_action_is_required(monkeypatch):
    """不帶動作就跑的話，絕對不可以「預設幫你裝一個」。

    變異測試記錄：把 `--install` 改成 `default=True` **survives**，而且是**等價
    變異**不是測試缺口——`add_mutually_exclusive_group(required=True)` 在讀 default
    之前就先擋掉空的 argv（實測兩種 default 都是 `SystemExit(2)`）。守門的是
    `required=True`，所以真正該釘的是它；`default` 在這條路上到不了。
    """
    monkeypatch.setattr(sys, "argv", ["install_autostart.py"])
    with pytest.raises(SystemExit) as caught:
        ia.main()
    assert caught.value.code != 0


# ===========================================================================
# `install` / `remove` / `status` —— 三支對外殼出命令的主指令
#
# 這個模組存在的理由寫在它自己的 docstring 裡：監督者撐得住「子行程掛掉」，撐不住
# 「整台機器掛掉」，而長時間無人值守的主機**就是會重開**。所以它是整條復原鏈的
# 最後一環。
#
# 2026-09-07 量覆蓋率時發現：8 支函式裡有 5 支一行都沒被跑過，其中就包含這三支。
# 它們全都是「組一串參數丟給 `schtasks`，再解析它的輸出」——**參數組錯或解析對不上
# 時不會有例外，只會安靜地什麼都沒做**，而下一次機器重開才會發現沒有東西被拉起來。
#
# 這裡把 `_schtasks` 換成假的，驗兩件事：送出去的參數長什麼樣，以及回來的輸出怎麼
# 被解讀。不開真的工作排程器（那會真的動到這台機器的排程）。
# ===========================================================================

class _FakeSchtasks:
    """記錄每一次呼叫，並依序回傳預先排好的結果。"""

    def __init__(self, *results):
        self.calls: list[tuple] = []
        self._queue = list(results)

    def __call__(self, *args):
        self.calls.append(args)
        if self._queue:
            return self._queue.pop(0)
        return _done(0)


def _done(returncode: int, stdout: str = "", stderr: str = ""):
    import subprocess
    return subprocess.CompletedProcess(
        args=["schtasks"], returncode=returncode, stdout=stdout, stderr=stderr)


# --- `status` ---------------------------------------------------------------

# 真實 `/FO LIST` 輸出的形狀（繁中 Windows）。欄位名是這支唯一的判準。
_ZH_LIST = r"""
資料夾: \Axiomatic
工作名稱:                                    \Axiomatic\Bot
下次執行時間:                                N/A
狀態:                                        就緒
登入模式:                                    僅互動式
上次執行時間:                                2026/9/3 下午 08:19:29
上次結果:                                    267009
執行工作:                                    D:\Work\Example\.venv\python.exe
開始位置:                                    D:\Work\Example
排程類型:                                    登入時
如果執行 X 小時又 X 分鐘，就停止工作:        停用
註解:                                        N/A
"""

_EN_LIST = r"""
Folder: \Axiomatic
TaskName:                             \Axiomatic\Bot
Next Run Time:                        N/A
Status:                               Ready
Logon Mode:                           Interactive only
Last Run Time:                        9/3/2026 8:19:29 PM
Last Result:                          267009
Task To Run:                          D:\Work\Example\.venv\python.exe
Start In:                             D:\Work\Example
Schedule Type:                        At logon
Stop Task If Runs X Hours and X Mins: Disabled
Comment:                              N/A
"""


def test_status_prints_the_fields_that_matter(monkeypatch, capsys):
    """欄位比對對不上時，這支會印出一片空白——看起來就像「工作沒註冊」。

    那正是 2026-09-03 踩過的形狀（當時是編碼餵錯，欄位名整排變成替換字元）。
    編碼那一半已經修好並寫在 `_schtasks` 的 docstring 裡；**解析這一半原本沒有
    任何測試**。
    """
    fake = _FakeSchtasks(_done(0, _ZH_LIST))
    monkeypatch.setattr(ia, "_schtasks", fake)
    assert ia.status(["bot"]) == 0
    out = capsys.readouterr().out
    for field in ("工作名稱", "狀態", "上次結果", "執行工作", "排程類型"):
        assert field in out, f"少了「{field}」這一欄：{out!r}"


def test_status_shows_the_seventy_two_hour_killer(monkeypatch, capsys):
    """「如果執行 X 小時…就停止工作」必須每次都看得到。

    排程器預設 72 小時就把工作砍掉，而這裡跑的是**連續好幾天**的批次——那個設定
    是致命的，所以它被刻意列進要顯示的欄位。掉了不會有人發現，直到某次批次在第
    三天無聲消失。
    """
    fake = _FakeSchtasks(_done(0, _ZH_LIST))
    monkeypatch.setattr(ia, "_schtasks", fake)
    ia.status(["bot"])
    assert "如果執行" in capsys.readouterr().out


def test_status_also_understands_an_english_windows(monkeypatch, capsys):
    """欄位名同時列中英兩套是刻意的——repo 不該綁死在一個地區設定。"""
    fake = _FakeSchtasks(_done(0, _EN_LIST))
    monkeypatch.setattr(ia, "_schtasks", fake)
    ia.status(["bot"])
    out = capsys.readouterr().out
    for field in ("TaskName", "Status", "Last Result", "Task To Run"):
        assert field in out, f"少了 {field}：{out!r}"


def test_status_says_not_registered_when_the_query_fails(monkeypatch, capsys):
    fake = _FakeSchtasks(_done(1, "", "ERROR: The system cannot find the file"))
    monkeypatch.setattr(ia, "_schtasks", fake)
    assert ia.status(["bot"]) == 0          # 查不到不是錯誤
    assert "未註冊" in capsys.readouterr().out


def test_status_queries_with_the_verbose_list_format(monkeypatch):
    """`/V /FO LIST` 缺一不可：少了 `/V` 就沒有那些欄位，少了 `/FO LIST` 是表格。"""
    fake = _FakeSchtasks(_done(0, _ZH_LIST))
    monkeypatch.setattr(ia, "_schtasks", fake)
    ia.status(["bot"])
    args = fake.calls[0]
    assert args[0] == "/Query"
    assert "/V" in args and "/FO" in args and "LIST" in args, args


# --- `remove` ---------------------------------------------------------------

def test_remove_forces_the_delete(monkeypatch, capsys):
    """`/F` 缺了的話 `schtasks` 會互動式問 Y/N，而這支跑在沒有 stdin 的情境。"""
    fake = _FakeSchtasks(_done(0))
    monkeypatch.setattr(ia, "_schtasks", fake)
    assert ia.remove(["bot"]) == 0
    assert fake.calls[0][0] == "/Delete" and "/F" in fake.calls[0]
    assert "已移除" in capsys.readouterr().out


@pytest.mark.parametrize("stderr, stdout", [
    ("ERROR: The system cannot find the file specified.", ""),
    ("", "錯誤: 系統找不到指定的檔案。"),
])
def test_removing_something_that_was_never_there_is_not_a_failure(
        monkeypatch, capsys, stderr, stdout):
    """「本來就沒有」跟「刪不掉」要分開，否則重複執行這支腳本會回非 0。"""
    fake = _FakeSchtasks(_done(1, stdout, stderr))
    monkeypatch.setattr(ia, "_schtasks", fake)
    assert ia.remove(["bot"]) == 0
    assert "本來就不存在" in capsys.readouterr().out


def test_a_real_removal_failure_is_reported(monkeypatch, capsys):
    fake = _FakeSchtasks(_done(1, "", "ERROR: Access is denied."))
    monkeypatch.setattr(ia, "_schtasks", fake)
    assert ia.remove(["bot"]) == 1
    assert "失敗" in capsys.readouterr().err


# --- `install` --------------------------------------------------------------

def test_install_registers_from_a_utf16_xml(monkeypatch, tmp_path, capsys):
    """`schtasks /XML` **只吃 UTF-16 帶 BOM**；餵 UTF-8 它只會說「載入工作 XML
    失敗」，不會說是編碼問題。這一支把寫出去的那個檔攔下來驗編碼。"""
    seen: dict = {}
    real_write = Path.write_text

    def _spy(self, data, *a, **kw):
        if str(self).endswith(".xml"):
            seen["encoding"] = kw.get("encoding")
            seen["text"] = data
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _spy)
    fake = _FakeSchtasks(_done(0))
    monkeypatch.setattr(ia, "_schtasks", fake)
    monkeypatch.setattr(ia, "_current_user", lambda: "DOMAIN\\user")

    assert ia.install(["bot"]) == 0
    assert seen.get("encoding") == "utf-16", seen.get("encoding")
    args = fake.calls[0]
    assert args[0] == "/Create" and "/XML" in args and "/F" in args, args
    assert "已註冊" in capsys.readouterr().out


def test_install_refuses_without_a_username(monkeypatch, capsys):
    """使用者名稱拿不到就不要註冊——XML 裡的 `UserId` 會是空的，
    工作註冊得起來卻永遠不會觸發。"""
    monkeypatch.setattr(ia, "_current_user", lambda: "")
    called = _FakeSchtasks()
    monkeypatch.setattr(ia, "_schtasks", called)
    assert ia.install(["bot"]) == 1
    assert called.calls == [], "沒有使用者名稱卻還是送了命令"
    assert "使用者名稱" in capsys.readouterr().err


def test_install_skips_a_missing_script(monkeypatch, capsys):
    """啟動器不在就跳過並回非 0——註冊一個指向不存在檔案的工作，
    只會在下次開機時安靜地失敗一次。"""
    monkeypatch.setattr(ia, "_current_user", lambda: "DOMAIN\\user")
    monkeypatch.setattr(ia, "TASKS", {"ghost": ("\\X\\Ghost", "no_such_launcher.py")})
    called = _FakeSchtasks()
    monkeypatch.setattr(ia, "_schtasks", called)
    assert ia.install(["ghost"]) == 1
    assert called.calls == []
    assert "找不到" in capsys.readouterr().err


def test_install_cleans_up_its_temp_xml(monkeypatch):
    """XML 裡有使用者名稱與完整路徑，不該留在 `%TEMP%`。"""
    made: list[str] = []
    real_unlink = os.unlink

    def _spy_unlink(path):
        made.append(str(path))
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _spy_unlink)
    monkeypatch.setattr(ia, "_schtasks", _FakeSchtasks(_done(0)))
    monkeypatch.setattr(ia, "_current_user", lambda: "DOMAIN\\user")
    ia.install(["bot"])
    assert made and made[0].endswith(".xml"), made
    assert not Path(made[0]).exists()


# --- `_current_user` --------------------------------------------------------

@pytest.mark.parametrize("domain, user, expected", [
    ("DOM", "alice", "DOM\\alice"),
    ("", "alice", "alice"),
    ("DOM", "", "DOM\\"),
])
def test_current_user_shapes(monkeypatch, domain, user, expected):
    monkeypatch.setenv("USERDOMAIN", domain)
    monkeypatch.setenv("USERNAME", user)
    assert ia._current_user() == expected


def test_the_schtasks_wrapper_uses_the_oem_code_page():
    """**指名編碼還得指對。** `schtasks` 是主控台程式，輸出走 OEM 代碼頁
    （這台是 cp950），不是 UTF-8。餵 `utf-8, errors="replace"` 不會拋——它會安靜地
    把每個中文欄位名變成替換字元，於是 `status` 的比對一條都對不上、印出一片空白，
    看起來像「工作沒註冊」。2026-09-03 實際踩到過。
    """
    import inspect
    import textwrap

    # **用 AST，不要用子字串。** 這支的第一版寫的是
    # `'"oem"' in inspect.getsource(...)`，而 `_schtasks` 的 docstring 本身就在
    # 解釋「要 `encoding="oem"` 不是 `"utf-8"`」——所以把程式碼改壞之後那個斷言
    # **照樣成立**。變異測試當場抓到它活了下來。
    tree = ast.parse(textwrap.dedent(inspect.getsource(ia._schtasks)))
    runs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and ast.unparse(n.func).endswith("subprocess.run")]
    assert len(runs) == 1, f"預期剛好一個 subprocess.run，實際 {len(runs)}"
    kwargs = {k.arg: ast.unparse(k.value) for k in runs[0].keywords}

    assert "oem" in kwargs.get("encoding", ""), (
        f"`schtasks` 的輸出編碼是 {kwargs.get('encoding')!r}——OEM 代碼頁被改掉了。"
        "餵 utf-8 不會拋，只會安靜地把中文欄位名變成替換字元，於是 `status` "
        "印出一片空白、看起來像「工作沒註冊」。")
    assert kwargs.get("errors") == "'replace'", (
        f"`errors` 是 {kwargs.get('errors')!r}——外部行程的輸出要容忍壞位元組。")
    assert kwargs.get("text") == "True", "沒有 text=True 的話拿到的是位元組"


# ===========================================================================
# 「上次結果」的解碼
# ===========================================================================
# `schtasks` 把這一欄印成裸的十進位整數。這支工具唯一的用途是回答「復原機制現在
# 是不是活的、上一次有沒有成功」，而一個裸數字對那個問題零貢獻——更糟的是，
# 「有人手動停掉它」（良性）與「帳戶資訊沒設定，工作永遠不會觸發」（致命）在畫面
# 上長得一模一樣，都是一串負數。

_STATUS_RUNNING = 267009            # 0x00041301 SCHED_S_TASK_RUNNING
_OPERATOR_REFUSED = -2147020576     # 0x800710E0 HRESULT_FROM_WIN32(4320)
_ACCOUNT_INFO_NOT_SET = -2147216625  # 0x8004130F SCHED_E_ACCOUNT_INFORMATION_NOT_SET


def test_a_zero_result_is_reported_as_success():
    assert "成功" in ia._explain_last_result("0")


def test_a_scheduler_status_code_is_decoded():
    """實測值：bot 那一筆就是 267009。"""
    hint = ia._explain_last_result(str(_STATUS_RUNNING))
    assert "0x00041301" in hint
    assert "排程器狀態" in hint


def test_the_code_that_means_it_will_never_fire_is_decoded():
    """`SCHED_E_ACCOUNT_INFORMATION_NOT_SET` 是這裡最該被看見的一個。

    工作註冊得起來、`--status` 顯示得出來、然後**永遠不會觸發**——正是本檔開頭
    講的那種安靜失效。它以裸數字呈現時，跟良性的「被人停掉」完全分不出來。
    """
    hint = ia._explain_last_result(str(_ACCOUNT_INFO_NOT_SET))
    assert "0x8004130F" in hint
    assert "排程器錯誤" in hint


def test_a_win32_facility_code_is_decoded():
    """實測值：批次那一筆是 -2147020576，解出來是「操作員已拒絕此要求」。

    看起來像出事了，其實是有人手動停掉。這正是不解碼就會誤判的方向。
    """
    hint = ia._explain_last_result(str(_OPERATOR_REFUSED))
    assert "0x800710E0" in hint
    assert "系統錯誤" in hint


def test_a_plain_exit_code_is_not_dressed_up_as_a_windows_error():
    """**這一支是整組裡最重要的。**

    `FormatMessageW` 對小整數一律回得出 Win32 錯誤訊息：`2` → 「系統找不到指定
    的檔案」。但「上次結果」那一欄放的可能是**被啟動的程式自己的結束碼**——
    `start_discord_bot.py` 自己 `sys.exit(2)` 的話，這裡就是 2。把它翻成「找不到
    檔案」是一個**聽起來完全合理、但錯的**解釋，而錯的解釋比沒有解釋更糟：它會
    把人帶去查一個不存在的問題。

    所以只有高位元組認得出來的 HRESULT 才解碼，其餘照實說「這是程式自己的結束
    碼」。這裡不比對訊息文字（那會綁死在系統語言），而是直接斷言**作業系統對這個
    數字講的那句話沒有出現在輸出裡**。
    """
    hint = ia._explain_last_result("2")
    assert "結束碼" in hint, hint
    os_message = ia._system_message(2)
    if os_message:                      # 非 Windows 拿不到訊息，那就沒什麼好比的
        assert os_message not in hint, (
            f"把程式的結束碼 2 講成了 Win32 錯誤「{os_message}」。")


def test_an_unknown_failure_hresult_is_still_flagged():
    """認不出設施碼、但高位元是 1 → 仍然要說它是個 HRESULT，不能當成結束碼。"""
    hint = ia._explain_last_result(str(-2147467259))     # 0x80004005 E_FAIL
    assert "0x80004005" in hint
    assert "結束碼" not in hint


@pytest.mark.parametrize("text", ["0x00041301", "  267009  ", "267009"])
def test_both_decimal_and_hex_are_accepted(text):
    """這台機器印十進位，但別的地區設定／版本印過十六進位。"""
    assert ia._parse_last_result(text) == 0x00041301


@pytest.mark.parametrize("text", ["", "   ", "不適用", "N/A", "abc"])
def test_an_unparsable_value_produces_no_claim(text):
    """解不出來就閉嘴。猜一個解釋比不解釋糟。"""
    assert ia._parse_last_result(text) is None
    assert ia._explain_last_result(text) == ""


@pytest.mark.skipif(os.name != "nt", reason="FormatMessageW 只有 Windows 有")
def test_the_explanation_text_comes_from_the_operating_system():
    """訊息文字向作業系統要，不自己維護一張表。

    自己抄一份就是「兩份平行清單，總有一份會被遺忘」，而抄錯的後果正是這組測試
    要防的毛病——一個講得頭頭是道但錯的解釋。實測 `FormatMessageW` 連
    `SCHED_S_*`／`SCHED_E_*` 都解得出來，所以沒有理由自己抄。
    """
    assert ia._system_message(0x00041301), (
        "作業系統解不出 SCHED_S_TASK_RUNNING；解碼器的前提不成立了。")


def test_asking_the_operating_system_never_raises():
    """報告工具不該在報告的路上自己炸掉。"""
    for code in (0, 0xFFFFFFFF, 0x0F00FFFF, _STATUS_RUNNING):
        ia._system_message(code)        # 不丟例外就算通過


def test_status_appends_the_explanation_to_the_real_field(monkeypatch, capsys):
    """端對端：解碼器要真的接在 `--status` 的輸出上，不是只有函式自己對。"""
    listing = (
        "工作名稱: \\Axiomatic\\Bot\n"
        "狀態: 執行中\n"
        f"上次結果: {_OPERATOR_REFUSED}\n"
    )
    monkeypatch.setattr(ia, "_schtasks", _FakeSchtasks(_done(0, listing)))
    assert ia.status(["bot"]) == 0
    out = capsys.readouterr().out
    assert str(_OPERATOR_REFUSED) in out, "原始數字不該被吃掉——它是可查的憑據"
    assert "0x800710E0" in out, f"沒有把解釋接上去：{out!r}"


def test_status_survives_a_task_scheduler_that_says_nothing_useful(
        monkeypatch, capsys):
    """欄位在、值卻是「不適用」時，狀態輸出照印，不因為解不出來就少一行。"""
    listing = "工作名稱: \\Axiomatic\\Bot\n上次結果: 不適用\n"
    monkeypatch.setattr(ia, "_schtasks", _FakeSchtasks(_done(0, listing)))
    assert ia.status(["bot"]) == 0
    assert "不適用" in capsys.readouterr().out


@pytest.mark.parametrize("label, patch_target, fake, fragment", [
    ("註冊端空了", "TASKS", {}, "`install_autostart.TASKS` 空了"),
    ("檢查端空了", "_AUTOSTART_TASKS", (), "`_AUTOSTART_TASKS` 空了"),
])
def test_each_autostart_floor_fires_on_its_own(monkeypatch, label,
                                               patch_target, fake, fragment):
    """兩道下限各給一份**剛好只違反它**的語料（§8.8(A4)）。

    這一支的原註解已經寫對了一半——「正面對照組要兩邊都做：任一邊空掉，下面那句
    差集會是空的、斷言永遠通過」。但那兩道下限**自己**沒有人在驗：把它們一起放寬
    成 0，整支照樣綠。而且兩道是**依序**的，所以一份「兩邊都空」的語料只會讓第一
    道炸——第二道一次都沒被執行過。所以這裡分成兩個案例，各只打壞一邊，並斷言
    **是哪一句在叫**。
    """
    import _process_control as pc

    if patch_target == "TASKS":
        monkeypatch.setattr(ia, "TASKS", fake)
    else:
        monkeypatch.setattr(pc, "_AUTOSTART_TASKS", fake)
    with pytest.raises(AssertionError) as excinfo:
        test_the_doctor_checks_exactly_the_tasks_that_get_registered()
    assert fragment in str(excinfo.value), (
        f"「{label}」紅的不是那一句，而是：{excinfo.value}")
