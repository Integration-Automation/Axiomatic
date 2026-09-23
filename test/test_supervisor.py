"""Tests for process supervisor backoff policy.

可直接 `py -3 test/test_supervisor.py`（自帶 runner），也可 pytest。

注意：匯入走「把套件目錄（`axiomatic/`）放進 sys.path，再直接 import `_supervisor`」
這條路（與其餘 test_*.py 一致），而**不是** `from axiomatic._supervisor import ...`。
後者只有在 repo root 也在 sys.path 上時才成立（pytest 回合由 `pytest.ini` 的
`pythonpath` 與 conftest 補上），單獨執行本檔會
`ModuleNotFoundError: No module named 'axiomatic'`。
"""
import ast
import contextlib
import gc
import importlib.util
import io
import os
import inspect
import pathlib
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))
# repo root 也要在路徑上：`other_launcher_pids` 內部走
# `from axiomatic._process_control import ...`，pytest 回合由 `pytest.ini` 的
# `pythonpath` 與 conftest 補上 repo root，但 `py -3 test/test_supervisor.py` 不會——
# 少了這一行，standalone 模式下那幾支會是 `ModuleNotFoundError: No module named 'axiomatic'`。
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _supervisor import (  # noqa: E402
    RC_ALREADY_RUNNING,
    RC_ZERO_PROGRESS,
    acquire_single_instance_lock,
    child_exit_is_fatal,
    restart_backoff,
)

PKG_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic")
REPO_ROOT = os.path.dirname(PKG_ROOT)
BOT_SCRIPT = os.path.join(PKG_ROOT, "discord_bot.py")
BOT_LOCK_FILE = os.path.join(REPO_ROOT, ".discord_bot.lock")
BOT_LAUNCHER = os.path.join(REPO_ROOT, "start_discord_bot.py")


def test_first_retry_waits_minimum_before_growing():
    assert restart_backoff(
        5, minimum=5, maximum=300, healthy=False
    ) == (5, 10)


def test_backoff_caps_and_healthy_run_resets():
    assert restart_backoff(
        200, minimum=5, maximum=300, healthy=False
    ) == (200, 300)
    assert restart_backoff(
        300, minimum=5, maximum=300, healthy=False
    ) == (300, 300)
    assert restart_backoff(
        300, minimum=5, maximum=300, healthy=True
    ) == (5, 5)


_INVALID_CONFIGS = [(0, 5), (-1, 5), (10, 5)]


@pytest.mark.parametrize(("minimum", "maximum"), _INVALID_CONFIGS)
def test_invalid_backoff_configuration_is_rejected(minimum, maximum):
    with pytest.raises(ValueError):
        restart_backoff(
            5, minimum=minimum, maximum=maximum, healthy=False
        )


def _lock_path(tmpdir) -> str:
    return os.path.join(str(tmpdir), "instance.lock")


def test_second_acquire_is_refused_while_the_first_still_holds(tmp_path):
    """這就是那次「兩套 bot 同時在跑」要擋掉的情境。"""
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None and not first.degraded
    try:
        assert acquire_single_instance_lock(path) is None
    finally:
        first.release()


def test_lock_is_reusable_once_the_holder_releases(tmp_path):
    """釋放後必須能重新取得——否則 supervisor 重啟一次就再也起不來。"""
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None
    first.release()
    second = acquire_single_instance_lock(path)
    assert second is not None and not second.degraded
    second.release()


def test_release_is_idempotent(tmp_path):
    """`finally: lock.release()` 可能在已釋放後再跑一次，不可以炸掉。"""
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    lock.release()
    lock.release()


def test_an_unusable_lock_file_lets_the_launcher_start_anyway(tmp_path):
    """判斷不出來時往「照常啟動」倒，不是往「拒絕啟動」倒。

    這條方向是刻意的，跟 CLAUDE.md 那條 PID 存活探測的保守方向相反：判錯成
    「拒絕」會讓 bot 因為一個無關的檔案系統問題完全不啟動、而且沒有人會發現；
    判錯成「放行」最多退回加這道鎖之前的狀態。回傳值必須是 degraded 的殼而
    **不是** None——None 是「已有實例」專用的答案。
    """
    path = os.path.join(str(tmp_path), "no_such_dir", "nested", "instance.lock")
    lock = acquire_single_instance_lock(path)
    assert lock is not None, "不可回 None——那會被 launcher 解讀成『已有實例』"
    assert lock.degraded is True
    lock.release()


def test_a_degraded_shell_can_still_be_released(tmp_path):
    """`degraded` 的空殼（`fd is None`）呼叫 `release()` 不得炸。

    啟動器的收尾路徑是無條件 `finally: lock.release()`，它分不出手上這個是真的鎖
    還是降級的殼。這一行炸掉的話，使用者看到的是收工時一整段 traceback，真正的
    結束原因被蓋在下面。
    """
    from _supervisor import InstanceLock
    shell = InstanceLock(None, _lock_path(tmp_path), degraded=True)
    shell.release()
    shell.release()             # 兩次也不炸


def test_instance_lock_must_not_grow_a_del_method():
    """`InstanceLock` **不可以**有 `__del__`。加上去會靜默放行第二個實例。

    鎖掛在 open file description 上，所以「關掉 fd」就等於「放掉鎖」。目前沒有
    `__del__`，因此丟掉參照只是洩漏一個 fd，鎖繼續持有到行程結束——互斥仍然成立
    （行為由 `test_dropping_the_reference_does_not_release_the_lock` 釘住）。

    這支存在的理由是那段 docstring 曾經反過來寫，說「一旦被回收，`__del__` 關掉
    fd 就等於放掉鎖」。一個維護者讀到它、發現類別上根本沒有 `__del__`，非常可能
    會「把它補完」——而補完之後就會**真的**產生那段話警告的缺陷：呼叫端只要沒把
    lock 存進變數（或那個變數在某次重構中不再被參照到），鎖就在無人察覺的情況下
    被放掉，第二個監督者起得來，兩個 webrunner 搶同一份 `.chrome_profile/`。

    要收 fd 的話請呼叫 `release()`，不要掛在物件生命週期上。
    """
    from _supervisor import InstanceLock
    assert "__del__" not in InstanceLock.__dict__, (
        "InstanceLock 長出了 __del__。物件被回收＝鎖被放掉＝第二個實例靜默起來，"
        "而且完全沒有訊息。收 fd 請用 release()，不要掛在物件生命週期上。")
    assert not hasattr(InstanceLock, "__del__"), (
        "InstanceLock 從某個基底類別繼承到了 __del__，後果同上。")


def test_dropping_the_reference_does_not_release_the_lock(tmp_path):
    """丟掉最後一個參照之後，鎖必須**還在**。

    這是上一支的行為面，兩支互補：屬性那支說得出原因（不要加 `__del__`），這支
    問的是**結果**而不是拼法，所以換一種寫法照樣抓得到。

    （順帶實測：`weakref.finalize(lock, ...)` 目前根本寫不出來——`__slots__` 裡沒有
    `__weakref__`，會丟 `TypeError: cannot create weak reference`。那是額外一層意外
    的保護，但**不要靠它**：有人往 `__slots__` 加一個 `__weakref__` 就沒了，而那看
    起來完全無害。這支測的是最終結果，加了也擋得住。）
    """
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    if lock.degraded:
        lock.release()
        pytest.skip("這台機器拿不到檔案鎖，測不出互斥")

    del lock
    gc.collect()
    gc.collect()                # 有參照循環時第二次才收得掉

    intruder = acquire_single_instance_lock(path)
    if intruder is not None:
        intruder.release()
    assert intruder is None, (
        "參照被回收之後鎖就放掉了——第二個實例現在起得來。八成是有人幫 "
        "InstanceLock 加了 __del__（或等價的 finalizer）。")


def test_the_already_running_rc_stays_distinguishable_from_a_crash():
    """這個 rc 唯一的用途就是「跟其他退出方式分得出來」。

    0 是正常結束、1 是未捕捉例外與一般失敗、2 是 CPython 自己的命令列錯誤——
    改成其中任何一個，supervisor 就會把「已有實例」誤判成一次普通崩潰然後照常
    重試，也就是這條規則要擋的那個空轉迴圈。
    """
    assert RC_ALREADY_RUNNING not in (0, 1, 2)
    assert child_exit_is_fatal(RC_ALREADY_RUNNING) is True
    for rc in (0, 1, 2, 137, 3221225477):
        assert child_exit_is_fatal(rc) is False


def test_the_launcher_actually_consumes_the_fatal_rc():
    """光是「bot 回 rc=3」沒有用——真正的失效模式是 supervisor 照樣重試。

    靜態檢查啟動器的 `main()` 裡真的有呼叫 `child_exit_is_fatal`。刪掉那段處理
    不會讓任何既有測試變紅（bot 照樣乾淨退出、退避數學照樣正確），但會讓啟動器
    每 5～300 秒重生一次註定被同一把鎖擋掉的子行程。
    """
    with open(BOT_LAUNCHER, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), BOT_LAUNCHER)
    main_fn = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    assert main_fn is not None, "start_discord_bot.py 沒有 main()"
    called = {
        node.func.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "child_exit_is_fatal" in called, (
        "start_discord_bot.main() 沒有檢查子行程的致命 rc；被鎖擋掉的 bot 會被"
        "當成普通崩潰無限重試。")


@pytest.mark.repo_write_ok(
    ".discord_bot.lock",
    reason="端對端契約本身就是『正式的實例鎖被握著時，直接執行 bot 會以 rc=3 被擋』；"
           "子行程的 bot 讀的是寫死在它自己模組裡的正式鎖路徑，導到暫存區就量不到。"
           "開這個檔只是嘗試上鎖，內容無關緊要，而且它本來就被 git 忽略。")
def test_running_the_bot_directly_is_refused_with_the_fatal_rc():
    """繞過啟動器直接執行 `discord_bot.py` 也要被擋掉，並用那個 rc 退出。

    這是整條契約唯一的端對端驗證：常數對不上、bot 沒接上鎖、或閘門被擺在會先
    失敗的東西後面，這裡都會紅。**驗證過會紅**：把 `main()` 的鎖檢查拿掉，這條
    會拿到 bot 正常啟動（或 token 錯誤）的 rc 而不是 3。
    """
    if importlib.util.find_spec("discord") is None:
        pytest.skip("這個直譯器沒有裝對話平台函式庫，跑不起 bot 本體")
    # 自己拿鎖來造出「已有實例」的情境。拿不到（`None` 專指已被別人持有，多半是
    # bot 正在跑）也一樣有效——那本來就是同一個情境，期望值不變，所以照跑不 skip。
    held = acquire_single_instance_lock(BOT_LOCK_FILE)
    if held is not None and held.degraded:
        held.release()
        pytest.skip("這台機器拿不到檔案鎖，測不出互斥")
    try:
        proc = subprocess.run(
            [sys.executable, BOT_SCRIPT],
            capture_output=True, text=True, timeout=180, cwd=REPO_ROOT,
            check=False,
            # bot 的訊息是 UTF-8 中文，而 Windows 上管線**兩端**都預設走本機
            # code page（cp950）。`encoding="utf-8"` 只管**解碼端**；子行程的
            # **編碼端**要靠 `PYTHONIOENCODING`，否則收到的是 cp950 位元組被
            # 當成 UTF-8 解，`errors="replace"` 再把它靜靜換成一串 U+FFFD。
            # （這段註解原本只講對了一半，2026-09-12 補上編碼端。）
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    finally:
        if held is not None:
            held.release()
    assert proc.returncode == RC_ALREADY_RUNNING, (
        f"直接執行 bot 本體時應該以 rc={RC_ALREADY_RUNNING} 被擋掉，實際 "
        f"rc={proc.returncode}。stderr 尾段：{(proc.stderr or '')[-800:]}")


# --------------------------------------------------------------------------
# DoD #5：直譯器探索順序（本地 `.venv` → `py -3` → `sys.executable`）
# --------------------------------------------------------------------------
# `CLAUDE.md` 說這個順序「必須完整保留——fresh clone 靠它」，但在這幾條之前**沒
# 有任何東西在檢查**，而它被寫了**兩份**（兩支啟動器各一），是典型會各自漂移的
# 配對。失敗形態一樣安靜：把 `.venv` 那一步拿掉，開發機照跑（系統直譯器剛好也
# 裝了東西），fresh clone 則會用一個沒有相依的直譯器去啟動，然後在 import 期死掉；
# 反過來把順序倒過來，正式行程會悄悄換成系統直譯器——那正是「三份相依版本」那個
# 陷阱的來源。
_LAUNCHERS = ("start_discord_bot.py", "start_webrunner.py")


def _python_command_node(launcher: str) -> ast.FunctionDef:
    """啟動器裡 `python_command()` 的 AST 節點（不靠文字切割）。"""
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "python_command":
            return node
    raise AssertionError(
        f"{launcher} 找不到 `python_command()`——DoD #5 的探索順序住在那裡，"
        "改名了就把這支測試一起更新，不要讓它靜默失效。")


def _discovery_order(launcher: str) -> list[str]:
    """`python_command()` 依**執行順序**回傳的候選。

    刻意看 `return` 的先後而不是字串在原始碼裡出現的位置：`venv_py = REPO_ROOT /
    '.venv' / …` 那一行永遠在最前面，所以照文字位置判斷的話，把 `.venv` 那個
    `if` 整段搬到 `py -3` 後面也照樣會過——順序被換掉卻沒人發現。
    """
    returns = [node for node in ast.walk(_python_command_node(launcher))
               if isinstance(node, ast.Return) and node.value is not None]
    order: list[str] = []
    # 依行號排序＝依原始碼順序。`ast.walk` 是廣度優先，巢狀在 `if` 裡的 return 會
    # 排在最外層那個 `return [sys.executable]` 後面，順序整個是錯的。這個函式是
    # 一串 guard clause，所以原始碼順序就是執行順序。
    for node in sorted(returns, key=lambda n: n.lineno):
        returned = ast.unparse(node.value)
        if "venv_py" in returned:
            order.append(".venv")
        elif "'-3'" in returned or '"-3"' in returned:
            order.append("py -3")
        elif "sys.executable" in returned:
            order.append("sys.executable")
    return order


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_the_interpreter_discovery_order_is_intact(launcher):
    """三個候選都要在，而且**執行順序**不能換。"""
    expected = [".venv", "py -3", "sys.executable"]
    actual = _discovery_order(launcher)
    assert actual == expected, (
        f"{launcher} 的 `python_command()` 探索順序是 {actual}，"
        f"DoD #5 要求 {expected}。順序就是規則本身：`.venv` 必須排第一，否則"
        "正式行程會悄悄換到系統直譯器上跑（相依版本當場分岔）；而沒有 `.venv` 的"
        "fresh clone 就靠後兩步才啟動得起來。")


def test_both_launchers_discover_the_interpreter_the_same_way():
    """兩份 `python_command()` 必須逐字相同——這是會各自漂移的配對。"""
    sources = {name: ast.unparse(_python_command_node(name))
               for name in _LAUNCHERS}
    first, second = _LAUNCHERS
    assert sources[first] == sources[second], (
        f"`{first}` 與 `{second}` 的 `python_command()` 不一樣了。兩支啟動器要用"
        "同一套探索順序，否則 bot 與 webrunner 會跑在不同的直譯器上——相依版本"
        "當場分岔，而且要到其中一邊 import 失敗才看得出來。"
        f"\n--- {first} ---\n{sources[first]}\n--- {second} ---\n{sources[second]}")




# ---------------------------------------------------------------------------
# 子行程輸出的落地：兩支啟動器都要有，實作只有一份
#
# 2026-08-23 webrunner 那一側踩過並修好：啟動器原本 `subprocess.run(cmd)`，子行程
# 直接繼承主控台，關掉視窗那行就永遠找不回來。當時只改了那一支，
# `start_discord_bot.py` 原封不動留到 2026-08-30——而 bot 那一側其實更嚴重，整條
# Secrecy Layer 1 就建立在「泛用訊息送 Discord、完整細節寫 log」上，bot 甚至會回
# 「請查看 log」，指向一個不存在的檔案。
# ---------------------------------------------------------------------------

_LAUNCHER_PATHS = {
    "start_discord_bot.py": os.path.join(REPO_ROOT, "start_discord_bot.py"),
    "start_webrunner.py": os.path.join(REPO_ROOT, "start_webrunner.py"),
}


def _called_names(path):
    """這支啟動器呼叫過的函式名（`f(...)` 與 `x.f(...)` 都算）。"""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.mark.parametrize("launcher", sorted(_LAUNCHER_PATHS))
def test_both_launchers_tee_the_child_into_a_log_file(launcher):
    """兩支啟動器都必須走 `stream_child`，而且都要修剪自己的 log。

    `subprocess.run(cmd)` 讓子行程直接繼承主控台——視窗一關就什麼都不剩，而這兩支
    印出來的東西（接續判定的 diverging fields、原始例外、supervisor 放棄原因）正是
    事後唯一能查的線索。
    """
    names = _called_names(_LAUNCHER_PATHS[launcher])
    assert "stream_child" in names, (
        f"{launcher} 沒有用 `stream_child` 起子行程。直接 `subprocess.run` 的話"
        "子行程繼承主控台，關掉視窗就什麼都不剩。")
    assert "trim_log" in names, (
        f"{launcher} 沒有呼叫 `trim_log`——附加式的 log 會無限長大。")


@pytest.mark.parametrize("launcher", sorted(_LAUNCHER_PATHS))
def test_no_launcher_runs_the_child_through_subprocess_run(launcher):
    """反面：`subprocess.run` 不得再用來跑被監督的子行程。

    這是原本的寫法，看起來完全正常，所以只有守門擋得住它回來。
    """
    with open(_LAUNCHER_PATHS[launcher], "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    hits = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"]
    assert not hits, (
        f"{launcher}:{hits} 又用 `subprocess.run` 跑子行程了——那條路沒有記錄檔。")


def _run_child(tmp_path, body, **kwargs):
    """把 `body` 寫成腳本、用 `stream_child` 跑它，回 `(rc, log 內容, 主控台)`。"""
    import contextlib
    import io
    import _supervisor as sup

    script = tmp_path / "child.py"
    script.write_text(body, encoding="utf-8")
    log_path = tmp_path / "child.log"
    console = io.StringIO()
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        with contextlib.redirect_stdout(console):
            rc = sup.stream_child([sys.executable, "-u", str(script)], handle,
                                  cwd=str(tmp_path), **kwargs)
    return rc, log_path.read_text(encoding="utf-8", errors="replace"), console.getvalue()


def test_stream_child_lands_stdout_stderr_and_rc(tmp_path):
    """stdout、stderr、rc 三者都要到位，而且主控台那一份也還在。

    合併 stderr 是刻意的：診斷訊息大半走 stderr，分兩個管線就要兩條抽水迴圈，而
    交錯的順序才是看得懂事情經過的關鍵。
    """
    rc, log, console = _run_child(tmp_path, (
        "import sys\n"
        "print('到 stdout 的中文')\n"
        "print('to stderr', file=sys.stderr)\n"
        "sys.exit(7)\n"))
    assert rc == 7, f"rc 沒傳回來：{rc}"
    assert "到 stdout 的中文" in log, log[:300]
    assert "to stderr" in log, "stderr 沒有被合併進來——診斷訊息大半走 stderr"
    assert "到 stdout 的中文" in console, "主控台那一份不見了；落地不該取代即時輸出"


def test_the_pump_does_not_deadlock_on_a_chatty_child(tmp_path):
    """輸出超過管線緩衝區（Windows 上約 64 KB）時不得卡死。

    給了 `stdout=PIPE` 卻沒人讀，子行程的下一個 print 就永遠卡住——那比原本沒有
    記錄檔更糟（批次會整個停住而不是掉一份 log）。抽水必須在自己的執行緒上。
    """
    rc, log, _console = _run_child(tmp_path, (
        "import sys\n"
        "for i in range(4000):\n"
        "    sys.stdout.write('x' * 40 + ' ' + str(i) + chr(10))\n"
        "sys.stdout.write('TAIL-MARKER' + chr(10))\n"
        "sys.exit(0)\n"))
    assert rc == 0
    assert log.count("x" * 40) == 4000, (
        f"只收到 {log.count('x' * 40)} / 4000 行——管線滿了之後卡住了。")
    assert "TAIL-MARKER" in log, "最後一行沒收到"


def test_a_bad_byte_from_the_child_does_not_stop_the_pump(tmp_path):
    """子行程吐出不合法的位元組時，後面的輸出還要繼續收。

    來源是外部（子行程可能印出任何東西），所以解碼端用 `errors="replace"`；一個
    怪位元組就讓抽水中斷的話，剩下的整段診斷都會不見。
    """
    _rc, log, _console = _run_child(tmp_path, (
        "import sys\n"
        "sys.stdout.buffer.write(bytes([0xff, 0xfe, 0x41]) + b'" + chr(92) + "n')\n"
        "sys.stdout.buffer.write('壞位元組之後這行還要看得到'.encode('utf-8') + b'"
        + chr(92) + "n')\n"
        "sys.stdout.buffer.flush()\n"))
    assert "壞位元組之後這行還要看得到" in log, log[:300]


def test_the_child_gets_utf8_io_encoding(tmp_path):
    """子行程強制 UTF-8 文字 I/O。

    管線不是主控台，CPython 會退回系統地區編碼（本機是 cp950），中文輸出就可能讓
    **子行程自己**炸掉——那是加了記錄檔反而把事情弄壞。

    先把父行程自己的 `PYTHONIOENCODING` 拿掉再量：不然這支在「開發者的殼剛好已經
    設了」的機器上會永遠綠，量到的是環境而不是程式碼（本機的殼就是 `UTF-8`）。
    """
    saved = os.environ.pop("PYTHONIOENCODING", None)
    try:
        _rc, log, _console = _run_child(tmp_path, (
            "import sys" + chr(10) +
            "print('ENC=' + (sys.stdout.encoding or '?').lower())" + chr(10)))
    finally:
        if saved is not None:
            os.environ["PYTHONIOENCODING"] = saved
    assert "enc=utf-8" in log.lower(), (
        f"子行程的 stdout 編碼不是 utf-8：{log[:200]!r}")


def test_a_wrong_pythonioencoding_in_the_parent_is_overridden(tmp_path):
    """呼叫端環境帶著一個**錯的** `PYTHONIOENCODING` 時，子行程仍然要吐 UTF-8。

    這是上一支的另一半，而且是唯一分得出「強制」與「讓步」的那一半。上一支先
    `os.environ.pop(...)` 再量（那是對的，理由白紙黑字寫在它的 docstring），但也
    因此只涵蓋「環境裡沒有這個變數」的情境——而在那個情境下
    `env.setdefault(...)`（讓步）與 `env[...] = ...`（覆寫）行為**一模一樣**，
    所以「把強制改成讓步」這個變異在 2026-09-12 之前活得下來。

    讓步的方向剛好是安靜壞掉的那一邊：`stream_child` 的解碼端是**寫死的**
    `encoding="utf-8", errors="replace"`，所以子行程若照 cp950／big5 編，回來的是
    一串 U+FFFD——沒有例外、沒有紅字、rc 也正常，只有 `webrunner.log` 裡的繁中
    進度行、resume 不符的原因、放棄理由整段變成問號。而啟動器最常見的起法（桌面
    捷徑、開機自動啟動、排程工作、別人的殼）正是「環境裡有一個我們沒設過的值」。

    量的是**子行程真的怎麼編碼**而不是傳下去的 env 字典：這支的覆蓋對象是
    `stream_child` 這個具名守門，而它存在的理由就是靜態掃描器看不出它開的是
    Python（命令列是呼叫端給的變數）。端到端量一次才算數。
    """
    saved = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "cp950"
    try:
        _rc, log, _console = _run_child(tmp_path, (
            "import sys" + chr(10) +
            "print('ENC=' + (sys.stdout.encoding or '?').lower())" + chr(10) +
            "print('繁中這一行要原樣回來')" + chr(10)))
    finally:
        if saved is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = saved
    assert "enc=utf-8" in log.lower(), (
        "呼叫端環境帶著 `PYTHONIOENCODING=cp950`，子行程就跟著用了它——這是"
        "讓步（`setdefault`）而不是覆寫，而解碼端是寫死的 utf-8。"
        f"子行程回報的編碼：{log[:200]!r}")
    assert "繁中這一行要原樣回來" in log, (
        f"兩端編碼不一致，繁中那一行沒有原樣回來：{log[:200]!r}")
    # 用 `chr(0xFFFD)` 而不是直接貼一個替代字元：原始碼裡擺一個真的 U+FFFD，讀起來會
    # 像這個檔案自己壞掉了，而且下一個編輯器可能順手把它「修正」掉。
    assert chr(0xFFFD) not in log, (
        f"記錄檔裡有 {log.count(chr(0xFFFD))} 個替代字元（U+FFFD），代表子行程"
        "照另一種編碼寫、我們照 utf-8 解。")


def test_log_lines_carry_a_timestamp_but_the_console_does_not(tmp_path):
    """記錄檔那份加時間戳（事後要跟 `events.ndjson` 的 ts 對得起來），主控台不加。"""
    import re
    _rc, log, console = _run_child(tmp_path, "print('MARK')\n")
    line = next(l for l in log.splitlines() if "MARK" in l)
    assert re.match(r"^\[\d\d-\d\d \d\d:\d\d:\d\d\] MARK", line), line
    assert console.splitlines()[0] == "MARK", (
        f"主控台那份被加了前綴：{console.splitlines()[0]!r}")


def test_say_goes_to_both_the_console_and_the_log(tmp_path):
    """監督者自己的訊息也要落地。

    放棄原因、rc、退避秒數正是事後診斷要看的東西，只印主控台等於沒留。
    """
    import contextlib
    import io
    import _supervisor as sup

    log_path = tmp_path / "say.log"
    console = io.StringIO()
    with log_path.open("a", encoding="utf-8", buffering=1) as handle:
        with contextlib.redirect_stdout(console):
            sup.say(handle, "supervisor: giving up after 5 attempts")
    text = log_path.read_text(encoding="utf-8")
    assert "giving up after 5 attempts" in text
    assert "giving up after 5 attempts" in console.getvalue()


def test_say_and_log_write_tolerate_no_log_at_all():
    """記錄檔開不起來時（唯讀磁碟、權限）啟動器照樣要能跑。

    `log=None` 是明確支援的狀態：落不了地就退回只印主控台，不能因此拒絕啟動。
    """
    import contextlib
    import io
    import _supervisor as sup

    console = io.StringIO()
    with contextlib.redirect_stdout(console):
        sup.say(None, "still speaks")
        sup.log_write(None, "swallowed\n")
    assert "still speaks" in console.getvalue()


def test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary(tmp_path):
    """修剪保留**尾段**、而且切在整行邊界。

    會炸掉的正是「崩潰 → 重生」那條接縫，所以要留最後那一段而不是最前面；切在
    半行上則會讓第一行變成無法解讀的碎片。
    """
    import _supervisor as sup

    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(20000)), encoding="utf-8")
    sup.trim_log(path, max_bytes=50_000, keep_bytes=20_000)
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_size <= 20_000, path.stat().st_size
    assert text.splitlines()[-1] == "line 19999", "尾段沒留住"
    assert text.splitlines()[0].startswith("line "), (
        f"第一行是半行碎片：{text.splitlines()[0]!r}")

    # 沒超過上限就一個位元組都不該動。
    before = path.read_bytes()
    sup.trim_log(path, max_bytes=10_000_000, keep_bytes=1000)
    assert path.read_bytes() == before, "沒超過上限卻被修剪了"


def test_trim_log_must_not_become_an_atomic_write(tmp_path):
    """`trim_log` **刻意**就地覆寫，不得改成「同目錄 temp → os.replace」。

    這條看起來跟 `CLAUDE.md` 的跨行程原子寫入硬規則衝突，所以要把理由寫死在這裡，
    否則遲早有人「順手修正」它——而 `webrunner.log` 確實是跨行程檔案（啟動器寫、
    bot 的 `/log tail` 讀）。

    差別在於**這個檔案同時有兩個活著的 append 控制代碼**：啟動器的 tee，以及
    第三方函式庫自己的 logger（它的檔名在 Windows 上與我們的只差大小寫，也就是
    **同一個檔案**——`Path("webrunner.log").resolve()` 指向磁碟上的 `WEBRunner.log`。
    實測那個檔案裡就同時有兩種格式的行）。

    2026-08-30 在這台機器上實測兩種寫法（結果就是下面這段在驗的）：
      * 就地覆寫：成功，而且另一個控制代碼**之後的 append 照樣落在可見的檔案裡**。
      * `os.replace`：直接 `PermissionError [WinError 5] 存取被拒`——Windows 不讓你
        取代一個別人開著的檔案。

    後者的下場不是「少了一次修剪」而是**永遠不再修剪**：`trim_log` 把 `OSError`
    吞掉只回傳，所以記錄檔會一路長下去，而且一個字都不會說。原子寫入在這裡不是
    更安全，是靜默失效。
    """
    import _supervisor as sup

    log = tmp_path / "app.log"
    log.write_text("old-line\n" * 400, encoding="utf-8")

    # 模擬第三方函式庫：以 append 模式握著同一個檔案不放。
    holder = open(log, "a", encoding="utf-8")
    try:
        holder.write("from-the-other-handle-1\n")
        holder.flush()

        sup.trim_log(log, max_bytes=200, keep_bytes=100)
        assert log.stat().st_size <= 200, "沒有修剪"

        # 修剪之後，另一個控制代碼仍然寫得進**可見的**檔案。
        holder.write("from-the-other-handle-2\n")
        holder.flush()
        text = log.read_text(encoding="utf-8")
        assert "from-the-other-handle-2" in text, (
            "修剪換掉了 inode，另一個行程的 append 掉進看不見的舊檔案了。"
            "trim_log 必須就地覆寫。")
    finally:
        holder.close()

    # 反面：確認 os.replace 在這個情境下真的會失敗——這支測試的理由建立在這件事上，
    # 哪天平台行為變了，這裡會紅，那就該重新評估整條規則而不是默默留著。
    if os.name == "nt":
        log2 = tmp_path / "app2.log"
        log2.write_text("x\n", encoding="utf-8")
        keeper = open(log2, "a", encoding="utf-8")
        try:
            tmp = tmp_path / "app2.log.tmp"
            tmp.write_bytes(b"replacement\n")
            try:
                os.replace(tmp, log2)
                raise AssertionError(
                    "Windows 現在允許取代開著的檔案了——本測試的前提變了，"
                    "重新評估 trim_log 該不該改成原子寫入，不要直接刪掉這一段。")
            except PermissionError:
                pass
        finally:
            keeper.close()


def test_trim_log_does_not_use_an_atomic_writer():
    """靜態面：`trim_log` 的原始碼裡不得出現 `os.replace`。

    上面那支驗的是行為，這支擋的是「看起來很對」的一行修改。兩支都要——行為那支
    需要平台配合，靜態這支在任何平台都會擋。
    """
    import _supervisor as sup

    source = inspect.getsource(sup.trim_log)
    assert "os.replace" not in source and "replace(" not in source, (
        "trim_log 用了 os.replace。這個檔案同時有兩個活著的 append 控制代碼，"
        "Windows 上 os.replace 會直接 PermissionError，而 trim_log 把 OSError "
        "吞掉——結果是記錄檔再也不會被修剪，且完全無聲。")


def test_trim_log_never_raises_on_a_missing_file(tmp_path):
    """記錄檔不存在／讀不到時只是不修剪——監督者不得因為記錄檔而死。"""
    import _supervisor as sup
    sup.trim_log(tmp_path / "nope.log", max_bytes=1, keep_bytes=1)


def main():
    # parametrize 過的案例在 standalone 時要自己展開，否則會少跑。
    import tempfile

    groups = [
        ("test_first_retry_waits_minimum_before_growing",
         test_first_retry_waits_minimum_before_growing),
        ("test_backoff_caps_and_healthy_run_resets",
         test_backoff_caps_and_healthy_run_resets),
        ("test_the_already_running_rc_stays_distinguishable_from_a_crash",
         test_the_already_running_rc_stays_distinguishable_from_a_crash),
        ("test_instance_lock_must_not_grow_a_del_method",
         test_instance_lock_must_not_grow_a_del_method),
        ("test_the_contended_errno_list_covers_both_platforms",
         test_the_contended_errno_list_covers_both_platforms),
        ("test_the_launcher_actually_consumes_the_fatal_rc",
         test_the_launcher_actually_consumes_the_fatal_rc),
        ("test_running_the_bot_directly_is_refused_with_the_fatal_rc",
         test_running_the_bot_directly_is_refused_with_the_fatal_rc),
    ]
    for name, fn in [
        ("test_second_acquire_is_refused_while_the_first_still_holds",
         test_second_acquire_is_refused_while_the_first_still_holds),
        ("test_lock_is_reusable_once_the_holder_releases",
         test_lock_is_reusable_once_the_holder_releases),
        ("test_release_is_idempotent", test_release_is_idempotent),
        ("test_an_unusable_lock_file_lets_the_launcher_start_anyway",
         test_an_unusable_lock_file_lets_the_launcher_start_anyway),
        ("test_a_degraded_shell_can_still_be_released",
         test_a_degraded_shell_can_still_be_released),
        ("test_dropping_the_reference_does_not_release_the_lock",
         test_dropping_the_reference_does_not_release_the_lock),
        # POSIX 分支那三支刻意不用 monkeypatch fixture，所以 standalone 也跑得到。
        ("test_the_posix_branch_takes_a_non_blocking_exclusive_flock",
         test_the_posix_branch_takes_a_non_blocking_exclusive_flock),
        ("test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd",
         test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd),
        ("test_the_posix_branch_without_fcntl_degrades_instead_of_refusing",
         test_the_posix_branch_without_fcntl_degrades_instead_of_refusing),
        ("test_an_oserror_without_an_errno_is_also_undecidable",
         test_an_oserror_without_an_errno_is_also_undecidable),
    ]:
        groups.append((
            name,
            lambda f=fn: [
                f(d) for d in [tempfile.mkdtemp(prefix="supervisor_test_")]
            ][0],
        ))
    # errno 白名單那兩組——這次改動的核心，standalone 也要跑得到。
    for errno_name, expected in _CONTENDED_ERRNOS:
        groups.append((
            "test_a_contended_lock_errno_still_means_another_instance"
            f"[{errno_name}]",
            lambda n=errno_name, e=expected: [
                test_a_contended_lock_errno_still_means_another_instance(n, e, d)
                for d in [tempfile.mkdtemp(prefix="supervisor_errno_")]
            ][0],
        ))
    for errno_name in _UNDECIDABLE_ERRNOS:
        groups.append((
            "test_an_unknown_lock_errno_degrades_instead_of_claiming_"
            f"another_instance[{errno_name}]",
            lambda n=errno_name: [
                test_an_unknown_lock_errno_degrades_instead_of_claiming_another_instance(  # noqa: E501
                    n, d)
                for d in [tempfile.mkdtemp(prefix="supervisor_errno_")]
            ][0],
        ))
    for minimum, maximum in _INVALID_CONFIGS:
        groups.append((
            f"test_invalid_backoff_configuration_is_rejected"
            f"[{minimum}-{maximum}]",
            lambda lo=minimum, hi=maximum:
                test_invalid_backoff_configuration_is_rejected(lo, hi),
        ))
    for launcher in _LAUNCHERS:
        groups.append((
            f"test_the_interpreter_discovery_order_is_intact[{launcher}]",
            lambda name=launcher:
                test_the_interpreter_discovery_order_is_intact(name),
        ))
    groups.append((
        "test_both_launchers_discover_the_interpreter_the_same_way",
        test_both_launchers_discover_the_interpreter_the_same_way))
    # DoD #5 的行為面：這幾支刻意用 `_Swapped` 而不是 monkeypatch fixture，
    # 就是為了 standalone runner 也跑得到（AST 守門看不到接線對不對）。
    for launcher in _LAUNCHERS:
        for os_name in sorted(_VENV_LAYOUT):
            groups.append((
                "test_a_clone_with_a_venv_runs_that_exact_interpreter"
                f"[{launcher}-{os_name}]",
                lambda n=launcher, o=os_name: [
                    test_a_clone_with_a_venv_runs_that_exact_interpreter(
                        n, o, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
            groups.append((
                "test_the_other_platforms_venv_layout_is_not_accepted"
                f"[{launcher}-{os_name}]",
                lambda n=launcher, o=os_name: [
                    test_the_other_platforms_venv_layout_is_not_accepted(
                        n, o, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
        for name, fn in [
            ("test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher",
             test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher),
            ("test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter",  # noqa: E501
             test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter),
            ("test_the_venv_beats_the_py_launcher_when_both_are_available",
             test_the_venv_beats_the_py_launcher_when_both_are_available),
        ]:
            groups.append((
                f"{name}[{launcher}]",
                lambda f=fn, n=launcher: [
                    f(n, pathlib.Path(d))
                    for d in [tempfile.mkdtemp(prefix="supervisor_py_")]
                ][0]))
    groups.append((
        "test_every_copy_of_python_command_is_accounted_for",
        test_every_copy_of_python_command_is_accounted_for))
    groups.append((
        "test_the_autostart_copy_is_deliberately_different_not_a_missed_one",
        test_the_autostart_copy_is_deliberately_different_not_a_missed_one))
    groups.append((
        "test_the_python_command_registration_bites_on_a_synthetic_corpus",
        test_the_python_command_registration_bites_on_a_synthetic_corpus))
    for launcher in sorted(_LAUNCHER_PATHS):
        groups.append((
            f"test_both_launchers_tee_the_child_into_a_log_file[{launcher}]",
            lambda name=launcher:
                test_both_launchers_tee_the_child_into_a_log_file(name)))
        groups.append((
            f"test_no_launcher_runs_the_child_through_subprocess_run[{launcher}]",
            lambda name=launcher:
                test_no_launcher_runs_the_child_through_subprocess_run(name)))
    for name, fn in [
        ("test_stream_child_lands_stdout_stderr_and_rc",
         test_stream_child_lands_stdout_stderr_and_rc),
        ("test_the_pump_does_not_deadlock_on_a_chatty_child",
         test_the_pump_does_not_deadlock_on_a_chatty_child),
        ("test_a_bad_byte_from_the_child_does_not_stop_the_pump",
         test_a_bad_byte_from_the_child_does_not_stop_the_pump),
        ("test_the_child_gets_utf8_io_encoding",
         test_the_child_gets_utf8_io_encoding),
        # 同一條規則的另一半——只有它分得出覆寫與 `setdefault`。
        ("test_a_wrong_pythonioencoding_in_the_parent_is_overridden",
         test_a_wrong_pythonioencoding_in_the_parent_is_overridden),
        ("test_log_lines_carry_a_timestamp_but_the_console_does_not",
         test_log_lines_carry_a_timestamp_but_the_console_does_not),
        ("test_say_goes_to_both_the_console_and_the_log",
         test_say_goes_to_both_the_console_and_the_log),
        ("test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary",
         test_trim_log_keeps_the_tail_and_cuts_on_a_line_boundary),
        ("test_trim_log_never_raises_on_a_missing_file",
         test_trim_log_never_raises_on_a_missing_file),
        ("test_trim_log_must_not_become_an_atomic_write",
         test_trim_log_must_not_become_an_atomic_write),
    ]:
        groups.append((
            name,
            lambda f=fn: [
                f(pathlib.Path(d))
                for d in [tempfile.mkdtemp(prefix="supervisor_log_test_")]
            ][0],
        ))
    groups.append((
        "test_say_and_log_write_tolerate_no_log_at_all",
        test_say_and_log_write_tolerate_no_log_at_all))
    groups.append((
        "test_trim_log_does_not_use_an_atomic_writer",
        test_trim_log_does_not_use_an_atomic_writer))
    for name, fn in [
        ("test_a_child_that_exits_in_time_is_not_terminated",
         test_a_child_that_exits_in_time_is_not_terminated),
        ("test_a_childs_own_exit_code_is_passed_through",
         test_a_childs_own_exit_code_is_passed_through),
        ("test_a_slow_child_gets_terminated_but_not_killed",
         test_a_slow_child_gets_terminated_but_not_killed),
        ("test_a_stuck_child_is_killed_last",
         test_a_stuck_child_is_killed_last),
        ("test_the_two_timeouts_are_not_swapped",
         test_the_two_timeouts_are_not_swapped),
        # 失敗路徑裡不吃 fixture 的那幾支。
        ("test_other_launcher_pids_without_psutil_returns_empty",
         test_other_launcher_pids_without_psutil_returns_empty),
        ("test_other_launcher_pids_skips_a_process_it_cannot_parse",
         test_other_launcher_pids_skips_a_process_it_cannot_parse),
        ("test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up",
         test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up),
        ("test_echo_line_falls_back_when_the_console_cannot_encode",
         test_echo_line_falls_back_when_the_console_cannot_encode),
        ("test_a_broken_console_does_not_stop_the_pump",
         test_a_broken_console_does_not_stop_the_pump),
        ("test_pump_stream_tolerates_no_stream_at_all",
         test_pump_stream_tolerates_no_stream_at_all),
        ("test_pump_stream_stops_quietly_when_the_pipe_dies",
         test_pump_stream_stops_quietly_when_the_pipe_dies),
    ]:
        groups.append((name, fn))
    skipped = 0
    for name, fn in groups:
        print(name)
        try:
            fn()
        except pytest.skip.Exception as reason:  # 環境限制，不是失敗
            skipped += 1
            print(f"  SKIP（{reason}）\n")
            continue
        print("  PASS\n")
    suffix = f"（{skipped} 筆因環境略過）" if skipped else ""
    # 這個 runner 是**逐支具名註冊**的，所以它一定會落後於檔案裡實際定義的測試——
    # 2026-09-03 實測 30/34，少的四支是吃 pytest fixture／parametrize 的那幾支，
    # 叫不動是應該的。問題不在少跑，而在**沒有講**：一句不帶條件的「ALL N PASSED」
    # 會被讀成「全部都過了」。所以這裡照 `test_bot_helpers` 的做法把差額印出來。
    try:
        declared = sum(
            1 for node in ast.parse(
                pathlib.Path(__file__).read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_"))
        missing = declared - len(groups)
        if missing > 0:
            suffix += (f"（另有 {missing} 支需要 pytest fixture／parametrize，"
                       "standalone 跑不到；完整結果請跑 pytest）")
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    print(f"ALL {len(groups) - skipped} TEST GROUPS PASSED{suffix}")
    return 0



# ---------------------------------------------------------------------------
# 兩支啟動器都要有單一實例鎖
#
# 2026-09-03 補。`start_discord_bot.py` 從 2026-08-30 就有一把，`start_webrunner.py`
# 一直沒有——純屬遺漏，而**沒鎖的那一側後果更重**：bot 本體自己還有第二把鎖兜著，
# webrunner 一層都沒有。兩個批次監督者同時跑 ⇒ 兩個 webrunner 搶同一份
# `.chrome_profile/`、互相 nuclear sweep、從同一組 `todo_*.md` 重複取件，兩邊看起來
# 都正常。這在接上「開機自動啟動」之後會從偶發變成常態（使用者手動開著、開機任務
# 又開一個），所以鎖是那條路的前置條件。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_takes_a_single_instance_lock(launcher):
    """用 AST 檢查真的有呼叫，不是只 import 進來擺著。

    這支測試存在的理由就是它抓到的那個不對稱：兩支啟動器長得幾乎一樣，少一把鎖
    從外面完全看不出來，而症狀（重複實例）看起來像是別的東西壞了。
    """
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "acquire_single_instance_lock" in called, (
        f"{launcher} 沒有取得單一實例鎖。兩支啟動器都必須擋掉「被啟動兩次」——"
        "webrunner 那一側尤其重要，它底下沒有第二層保護。")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_locks_its_own_file(launcher):
    """一支程式一個鎖檔。共用的話啟動器會把自己 spawn 的子行程擋掉。"""
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = {node.targets[0].id: ast.unparse(node.value)
             for node in ast.walk(tree)
             if isinstance(node, ast.Assign) and len(node.targets) == 1
             and isinstance(node.targets[0], ast.Name)}
    assert "LOCK_FILE" in names, f"{launcher} 沒有定義 LOCK_FILE"


def test_the_two_launchers_do_not_share_a_lock_file():
    """共用一個鎖檔 ＝ 先起來的那一支把另一支永久擋掉。"""
    seen = {}
    for launcher in _LAUNCHERS:
        path = os.path.join(REPO_ROOT, launcher)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "LOCK_FILE"):
                seen[launcher] = ast.unparse(node.value)
    assert len(seen) == len(_LAUNCHERS), f"有啟動器沒有 LOCK_FILE：{seen}"
    assert len(set(seen.values())) == len(seen), (
        f"兩支啟動器共用同一個鎖檔：{seen}。這會讓其中一支永遠啟動不了。")
    # bot 本體自己那一把也不得與啟動器共用（見 acquire_single_instance_lock）。
    assert ".discord_bot.lock" not in {v.split('/')[-1].strip('\'"')
                                       for v in seen.values()}


def test_other_launcher_pids_ignores_a_process_that_merely_mentions_the_name():
    """子字串比對會把「命令列裡剛好提到檔名」的 shell 與 `python -c` 算成實例。

    這條規則在 `_process_control` 那邊實測過 7 筆裡有 5 筆是這種；搬上來共用之後
    要確定沒有在搬家途中掉了。
    """
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe", ["python.exe", "-c", "print('start_webrunner.py')"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == []


def test_other_launcher_pids_finds_a_real_second_instance():
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe",
              ["C:/py/python.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == [11]


def test_other_launcher_pids_excludes_itself():
    """自己不是「另一個實例」。"""
    from _supervisor import other_launcher_pids
    procs = [(99, "python.exe",
              ["C:/py/python.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99, procs=procs) == []


def test_other_launcher_pids_collapses_the_shim_and_the_real_interpreter():
    r"""`.venv\Scripts\python.exe` 是轉接殼，會 spawn 真的直譯器：cmdline 一模一樣、
    父子關係。不併的話一個既有實例會被報成兩個 pid，讀的人以為自己開了兩份。"""
    from _supervisor import other_launcher_pids
    cmd = ["D:/Work/Example/.venv/Scripts/python.exe",
           "D:/Work/Example/start_webrunner.py"]
    procs = [(11, "python.exe", cmd, 1),      # 轉接殼
             (12, "python.exe", cmd, 11)]     # 本尊（parent = 11）
    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=procs) == [11]


def test_other_launcher_pids_never_raises_on_junk():
    """診斷用的東西不該把啟動器弄掛。"""
    from _supervisor import other_launcher_pids
    for junk in ([(None, None, None, None)], [(1, "python.exe", None, 0)], []):
        assert other_launcher_pids("start_webrunner.py", self_pid=9,
                                   procs=junk) == []


def _load_launcher(name):
    """把啟動器當模組載進來（不執行 `main()`）。"""
    path = os.path.join(REPO_ROOT, name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_held_lock_stops_the_launcher_before_it_spawns_anything(
        launcher, tmp_path, monkeypatch):
    """鎖被別人持有時，啟動器必須在 spawn **之前**就退出。

    AST 那兩支只證明「有呼叫、鎖檔不同」，證明不了接線對不對——`if lock is None`
    寫反了照樣通過。這支從 `main()` 進去實跑一遍，斷言的是「監督迴圈根本沒被
    呼叫」，也就是「沒有第二個瀏覽器堆疊被開出來」這件事本身。

    刻意**不**用子行程跑真的啟動器：萬一鎖沒擋住，真的會 spawn 出一個 webrunner，
    而 webrunner 一啟動就無條件 nuclear sweep 掉所有 chrome——正式批次的瀏覽器會
    當場被殺。測一個安全機制不該冒它要防的那個險。
    """
    module = _load_launcher(launcher)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG" if "webrunner" in launcher
                        else "BOT_LOG", tmp_path / "test.log")
    monkeypatch.setattr(sys, "argv", [launcher])

    # 攔截器**丟例外**而不是回一個 rc。回 rc 的話，bot 那一支的 `while True:`
    # 監督迴圈會把「子行程乾淨結束」當成要重生，於是每 5 秒轉一圈永遠不停——
    # 這支測試在變異測試裡就是這樣掛住的（實際踩到）。掛住的測試比紅掉的測試更
    # 糟：紅的會指出問題，掛住的只是讓整輪停在那裡。
    class _Spawned(Exception):
        pass

    for hook in ("_supervise", "stream_child"):
        if hasattr(module, hook):
            def _boom(*_a, **_k):
                raise _Spawned
            monkeypatch.setattr(module, hook, _boom)

    held = acquire_single_instance_lock(tmp_path / "test.lock")
    if held is None or held.degraded:
        if held is not None:
            held.release()
        pytest.skip("這台機器拿不到檔案鎖，測不出互斥")
    try:
        try:
            rc = module.main()
        except _Spawned:
            pytest.fail(
                f"{launcher} 在鎖已被持有的情況下**還是 spawn 了**。這正是這把鎖"
                "要擋的：兩套堆疊同時跑，兩邊看起來都正常。")
    finally:
        held.release()

    assert rc != 0, f"{launcher} 被鎖擋下卻回報成功（rc={rc}）"


# ---------------------------------------------------------------------------
# Ctrl+C 之後的收屍（`reap_child`）
#
# 2026-09-05 量覆蓋率時發現這一支**一行都沒被跑過**。它是「先禮後兵」那條路：
# 等 → terminate → kill。寫錯的後果不會當場報錯，而是留下孤兒子行程——webrunner
# 那一側連帶留下整棵 Chrome，下一次啟動就變成兩套堆疊搶同一份 `.chrome_profile/`。
# 用假的 proc 驗，不開真的行程：這裡要釘的是**順序與逾時值**，不是作業系統行為。
# ---------------------------------------------------------------------------

class _FakeProc:
    """`wait()` 依腳本逐次回應：`"timeout"` 丟 TimeoutExpired，數字就當 rc 回傳。"""

    def __init__(self, script):
        self._script = list(script)
        self.waits = []          # 每次 wait 收到的 timeout（None ＝沒給）
        self.calls = []          # terminate / kill / wait 的先後順序

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.calls.append("wait")
        step = self._script.pop(0)
        if step == "timeout":
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)
        return step

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def test_a_child_that_exits_in_time_is_not_terminated():
    import _supervisor as sup

    proc = _FakeProc([0])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == 0
    assert proc.calls == ["wait"], (
        "子行程自己收工了卻還是被 terminate／kill：" + repr(proc.calls))


def test_a_childs_own_exit_code_is_passed_through():
    """收屍不能把 rc 吃掉——監督者要靠它分辨「自己停的」與「掛了」。"""
    import _supervisor as sup

    proc = _FakeProc([3])
    assert sup.reap_child(proc, None, grace_sec=1.0, kill_sec=1.0) == 3


def test_a_slow_child_gets_terminated_but_not_killed():
    import _supervisor as sup

    proc = _FakeProc(["timeout", 0])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == 0
    assert proc.calls == ["wait", "terminate", "wait"], repr(proc.calls)
    assert "kill" not in proc.calls, (
        "terminate 之後就收工了，不該再 kill——kill 等於 TerminateProcess，"
        "子行程的 finally 完全不會跑")


def test_a_stuck_child_is_killed_last():
    import _supervisor as sup

    proc = _FakeProc(["timeout", "timeout", -9])
    rc = sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert rc == -9
    assert proc.calls == ["wait", "terminate", "wait", "kill", "wait"], (
        repr(proc.calls))


def test_the_two_timeouts_are_not_swapped():
    """寬限期給第一次等待、kill 逾時給第二次，最後那一次**不設逾時**。

    對調不會有任何症狀——兩個都是正數，流程照跑——但意思整個相反：本來要給子行程
    30 秒收尾的，變成只給 10 秒。最後一次若也帶逾時則更糟：kill 之後還可能丟
    TimeoutExpired 出去，收屍反而變成拋例外。
    """
    import _supervisor as sup

    proc = _FakeProc(["timeout", "timeout", 0])
    sup.reap_child(proc, None, grace_sec=30.0, kill_sec=10.0)
    assert proc.waits == [30.0, 10.0, None], repr(proc.waits)


# ===========================================================================
# 失敗路徑（2026-09-07 補齊）
#
# 量覆蓋率時（全套 2742 passed）不看百分比、只問「哪些 `except` 分支一行都沒被
# 執行過」，答案是：**這個模組的每一個錯誤處理器都沒有**。監督者存在的唯一理由
# 就是「別的東西壞掉的時候撐住」，所以這等於它最核心的職責從來沒被驗證過。而它
# 是整條復原鏈的根——它自己在處理錯誤的路上掛掉，上面所有東西一起停，**而且不會
# 有任何錯誤訊息**，因為會印訊息的那個東西就是掛掉的那個。
#
# 補的過程中抓到兩個真缺陷，各自有對應的行為測試（不是靜態掃描）：
#   * `echo_line` 的退版寫入不在任何保護之下（見
#     `test_a_console_that_breaks_during_the_fallback_never_escapes`）。
#   * `stream_child` 的 spawn hook 失敗會留下孤兒子行程（見
#     `test_a_failing_spawn_hook_does_not_leave_an_orphan`）。
# ===========================================================================


class _NoModule:
    """讓 `import <name>` 真的丟 `ImportError` 的 context manager。

    **一定要寫 `sys.modules[name] = None`，不可以 `pop`／`del`。** `pop` 只是清掉
    快取，接下來的 `import` 會從磁碟重新載入**真的那一個**——2026-09-07 就是這樣
    讓一支「模擬 psutil 不存在」的測試拿到真的 psutil，然後 `proc.kill()` 殺掉了
    這台機器上一個跑了 78.7 小時的正式批次的 Chrome，而測試表面上只是斷言失敗。
    CPython 看到 `sys.modules[name] is None` 會直接丟 `ImportError`、不會去找
    檔案，那才是「不存在」。
    """

    _ABSENT = object()

    def __init__(self, *names):
        self._names = names
        self._saved = {}

    def __enter__(self):
        for name in self._names:
            self._saved[name] = sys.modules.get(name, self._ABSENT)
            sys.modules[name] = None
        return self

    def __exit__(self, *_exc):
        for name, saved in self._saved.items():
            if saved is self._ABSENT:
                sys.modules.pop(name, None)   # 本來就不在，還原＝拿掉
            else:
                sys.modules[name] = saved
        return False


# ---------------------------------------------------------------------------
# 單一實例鎖：用**兩個真的行程**驗互斥
# ---------------------------------------------------------------------------
#
# 既有的那支（`test_second_acquire_is_refused_while_the_first_still_holds`）是同一
# 個行程開兩個 fd。那證明得了「同一支程式不能自己鎖兩次」，證明不了這把鎖真正要
# 擋的情境——**開機自動啟動的那一份，與使用者自己開的那一份，是兩個行程**。
# Windows 的 `msvcrt.locking` 與 POSIX 的 `flock` 都是掛在 open file description
# 上，同行程／跨行程的語意本來就可能不同，所以要跨行程才算驗過。
#
# 鎖檔一律開在 `tmp_path`：正式的 `.webrunner_supervisor.lock` /
# `.discord_bot_supervisor.lock` 摸都不要摸，這台機器上有長跑中的正式批次。

_LOCK_PROBE_SOURCE = '''
"""一次性探針：試著取得單一實例鎖，把結果寫進 verdict 檔。"""
import os
import pathlib
import sys
import time

sys.path.insert(0, sys.argv[1])
from _supervisor import acquire_single_instance_lock

lock_path, verdict_path, go_path, mode = sys.argv[2:6]
lock = acquire_single_instance_lock(lock_path)
if lock is None:
    verdict = "REFUSED"
elif lock.degraded:
    verdict = "DEGRADED"
else:
    verdict = "ACQUIRED"
tmp = verdict_path + ".tmp"
pathlib.Path(tmp).write_text(verdict, encoding="utf-8")
os.replace(tmp, verdict_path)

if mode == "hold" and verdict == "ACQUIRED":
    # 等父行程放行。60 秒是**自保上限**：測試中途被砍也不會留下一個握著鎖的孤兒。
    deadline = time.time() + 60.0
    while time.time() < deadline and not pathlib.Path(go_path).exists():
        time.sleep(0.02)

if lock is not None:
    lock.release()
'''


def _spawn_lock_probe(script, tmp_path, lock_file, tag, *, mode="try"):
    """起一個真的行程去搶 `lock_file`，回 `(proc, verdict 檔)`。"""
    verdict = tmp_path / (tag + ".verdict")
    proc = subprocess.Popen(
        [sys.executable, str(script), PKG_ROOT, str(lock_file), str(verdict),
         str(tmp_path / "go"), mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 子行程的診斷是 UTF-8，但管線**兩端**在 Windows 上都預設走 cp950。
        # `encoding="utf-8"` 只管解碼端，子行程的編碼端要靠 `PYTHONIOENCODING`
        # ——`_supervisor.stream_child` 從一開始就是這樣做的，這裡補齊。
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return proc, verdict


def _read_verdict(path, *, seconds=60.0):
    """等 verdict 檔出現並回內容；逾時回 None（**有界**，不會把整輪卡住）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        time.sleep(0.02)
    return None


def test_a_second_process_really_cannot_take_the_lock(tmp_path):
    """兩個**真的行程**同時要這把鎖，只有一個拿得到；持有者退出後才輪得到別人。

    這正是 2026-09-03 補 `.webrunner_supervisor.lock` 的理由：兩個批次監督者同時
    跑 ⇒ 兩個 webrunner 搶同一份 `.chrome_profile/`、各自 nuclear sweep 把對方的
    Chrome 殺掉、從同一組 `todo_*.md` 重複取件，而**兩邊的 log 看起來都正常**。

    設計成「持有者先就位，挑戰者才起跑」而不是「三個一起搶」，是為了不引入時序
    競賽：真的同時起跑的話，贏家可能在輸家還沒開始前就釋放了，於是偶爾兩個都
    ACQUIRED——一支會隨機紅的守門，最後一定會被當成雜訊關掉。
    """
    script = tmp_path / "lock_probe.py"
    script.write_text(_LOCK_PROBE_SOURCE, encoding="utf-8")
    lock_file = tmp_path / "cross_process.lock"
    go = tmp_path / "go"

    holder, holder_verdict = _spawn_lock_probe(
        script, tmp_path, lock_file, "holder", mode="hold")
    try:
        first = _read_verdict(holder_verdict)
        if first == "DEGRADED":
            pytest.skip("這台機器拿不到檔案鎖，測不出互斥")
        assert first == "ACQUIRED", (
            f"持有者行程沒有拿到鎖（verdict={first!r}）")

        for tag in ("other1", "other2"):
            proc, verdict_path = _spawn_lock_probe(
                script, tmp_path, lock_file, tag)
            output = proc.communicate(timeout=120)[0]
            assert proc.returncode == 0, f"探針自己壞了：{output}"
            assert _read_verdict(verdict_path, seconds=10) == "REFUSED", (
                f"{tag}：鎖已被另一個行程持有，第二個實例卻拿到了。這把鎖擋的就是"
                "「開機自動啟動的那份 ＋ 使用者自己開的那份」同時在跑。")
    finally:
        go.write_text("go", encoding="utf-8")
        try:
            holder.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate()

    # 持有者結束 ⇒ OS 立刻放掉鎖，下一個行程必須拿得到。拿不到就代表殘留了一把
    # 誰也解不開的鎖，開機自動啟動會從此永遠起不來。
    proc, verdict_path = _spawn_lock_probe(script, tmp_path, lock_file, "after")
    output = proc.communicate(timeout=120)[0]
    assert proc.returncode == 0, f"探針自己壞了：{output}"
    assert _read_verdict(verdict_path, seconds=10) == "ACQUIRED", (
        "持有者已經結束，鎖卻還是拿不到——殘留鎖會讓啟動器再也起不來。")


def test_a_platform_without_file_locking_still_starts(tmp_path):
    """沒有 `msvcrt`／`fcntl` 時往「照常啟動」倒，而且**不可以**回 None。

    判錯成「拒絕」會讓啟動器因為一個與它無關的平台問題完全不啟動、而且沒有人會
    發現；判錯成「放行」最多退回加這把鎖之前的狀態。`None` 是「已有實例」專用的
    答案，這條路回它就等於謊報。
    """
    path = _lock_path(tmp_path)
    with _NoModule("msvcrt", "fcntl"):
        lock = acquire_single_instance_lock(path)
        assert lock is not None, "不可回 None——那會被啟動器解讀成『已有實例』"
        assert lock.degraded is True, "鎖機制不可用時必須誠實標記成 degraded"
        # degraded 是「沒有互斥保證」的殼，所以第二個也會拿到。這是刻意的取捨，
        # 寫在這裡是為了讓下一個讀的人知道它不是漏掉。
        second = acquire_single_instance_lock(path)
        assert second is not None and second.degraded is True
        second.release()
    lock.release()


def test_refusing_a_second_instance_survives_a_failing_close(tmp_path):
    """讓位那條路上連 `os.close` 都失敗，仍然要乾淨地回 `None`。

    這是「已經有另一個實例」唯一的出口。它在收尾時 `os.close(fd)`，而 close 是
    會失敗的（fd 早被別的東西回收、網路磁碟丟 EIO）。沒接住的話，第二個實例不是
    印出「已經有另一個實例在執行」而是吐一整段 traceback ——使用者看到的是
    「啟動器壞了」，不是「本來就不該開第二個」。
    """
    path = _lock_path(tmp_path)
    first = acquire_single_instance_lock(path)
    assert first is not None
    if first.degraded:
        first.release()
        pytest.skip("這台機器拿不到檔案鎖，測不出互斥")

    real_open, real_close = os.open, os.close
    ours: set[int] = set()

    def _fake_open(file, flags, mode=0o777, **kwargs):
        fd = real_open(file, flags, mode, **kwargs)
        if str(file) == str(path):
            ours.add(fd)
        return fd

    def _fake_close(fd):
        if fd in ours:
            ours.discard(fd)
            real_close(fd)      # 真的關掉，不然這支測試自己會漏 fd
            raise OSError(9, "Bad file descriptor")
        return real_close(fd)

    os.open, os.close = _fake_open, _fake_close
    try:
        second = acquire_single_instance_lock(path)
    finally:
        os.open, os.close = real_open, real_close
        first.release()
    assert second is None, (
        "第二個實例應該安靜地拿到 None；收尾時的 close 失敗不該讓它爆出例外。")


def test_release_survives_a_close_that_fails(tmp_path):
    """`finally: lock.release()` 在 fd 已經不見時也不得丟例外。

    `release` 的註解說它只是禮貌性收尾（OS 在行程結束時本來就會放），所以它**更
    不該**是啟動器收工路上唯一會炸的一行——那會把真正的結束原因蓋掉。
    """
    path = _lock_path(tmp_path)
    lock = acquire_single_instance_lock(path)
    assert lock is not None
    # 從背後把 fd 關掉，`release` 之後那次 close 就會拿到 EBADF。
    os.close(lock._fd)          # noqa: SLF001  # pylint: disable=protected-access
    lock.release()              # 不得丟例外
    again = acquire_single_instance_lock(path)
    assert again is not None, "fd 都關了，鎖必須真的放掉"
    again.release()


# ---------------------------------------------------------------------------
# POSIX 的 `fcntl.flock` 分支——在 Windows 上照樣測得到（2026-09-08）
# ---------------------------------------------------------------------------
#
# 曾有一筆待辦寫著這條分支「在一般的機器上永遠到不了，判定完成需要一台
# Linux」。**前提是錯的。** kernel 真正的 flock 語意本來就不是我們該測的東西；我們
# 自己寫的那一段全是純 Python，換掉 `os.name` 與 `fcntl` 兩個相依就跑得完：
#
#   * 有沒有走對分支（`os.name` 判斷）；
#   * 旗標是不是 `LOCK_EX | LOCK_NB`——漏掉 `LOCK_NB` 會變成**阻塞版**，第二個實例
#     不是印一行「已經有另一個實例在執行」然後退出，而是安靜地卡在那裡等到天荒地老，
#     而開機自動啟動那份就這樣掛著；
#   * 交給 flock 的是不是 `os.open` 拿到的那個 fd；
#   * `OSError` 有沒有映射成 `None` 並把 fd 關掉；
#   * `ImportError` 有沒有降級成殼而**不是** `None`。
#
# 教訓比這幾支測試本身值錢：「這個分支只有在別的作業系統上才跑得到」聽起來像事實，
# 其實只是還沒想到怎麼把那個作業系統條件替換掉——跟 CLAUDE.md 那條「不要拿不存在
# 的限制當理由推掉改動」是同一個形狀。

try:
    import msvcrt as _REAL_MSVCRT
except ImportError:             # 非 Windows
    _REAL_MSVCRT = None

_MISSING = object()


class _PosixOs:
    """讓 `_supervisor` 眼中的 `os.name` 變成 `"posix"`，其餘一律轉給真的 `os`。

    刻意**不**寫 `setattr(os, "name", "posix")`：那會改到整個行程看到的 `os.name`，
    本檔還有 daemon 抽水執行緒在跑，不該連它們一起騙。換掉的是
    `_supervisor` 模組自己那個 `os` 名字，範圍剛好是被測的那一段。
    """

    name = "posix"

    def __getattr__(self, attr):
        return getattr(os, attr)


class _RecordingPosixOs(_PosixOs):
    """再加上記帳：哪些 fd 被開、哪些被關。

    測「fd 有沒有被關掉」不去翻 `lock._fd` 這種私有欄位——那會讓測試綁死在實作的
    欄位名上；記 `os.close` 的呼叫問的才是行為本身。
    """

    def __init__(self):
        self.opened: list[int] = []
        self.closed: list[int] = []

    def open(self, file, flags, mode=0o777, **kwargs):
        fd = os.open(file, flags, mode, **kwargs)
        self.opened.append(fd)
        return fd

    def close(self, fd):
        self.closed.append(fd)
        return os.close(fd)


class _FakeFcntl:
    """假的 `fcntl`：記下 `flock` 的呼叫，其餘什麼都不做。

    常數用 POSIX 的真值（`LOCK_EX=2` / `LOCK_NB=4`），所以「只傳 `LOCK_EX`」的
    阻塞版退化會得到 2 而不是 6——分得出來。
    """

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self, error: OSError | None = None):
        self.calls: list[tuple[int, int]] = []
        self._error = error

    def flock(self, fd, operation):
        self.calls.append((fd, operation))
        if self._error is not None:
            raise self._error


def _msvcrt_must_not_be_used(*_args, **_kwargs):
    raise AssertionError(
        "走到 Windows 分支了——`os.name` 的判斷壞了，POSIX 那一半根本沒被測到。")


class _Swapped:
    """`setattr` 版的 try/finally。**刻意不用 `monkeypatch` fixture**：本檔的
    standalone runner（`py -3 test/test_supervisor.py`）沒有 pytest fixture，
    用 fixture 的話這幾支在那條路上會整組消失。
    """

    def __init__(self, obj, attr, value):
        self._obj, self._attr, self._value = obj, attr, value
        self._saved = _MISSING

    def __enter__(self):
        self._saved = getattr(self._obj, self._attr, _MISSING)
        setattr(self._obj, self._attr, self._value)
        return self

    def __exit__(self, *_exc):
        if self._saved is _MISSING:
            delattr(self._obj, self._attr)
        else:
            setattr(self._obj, self._attr, self._saved)
        return False


class _FakeModule:
    """把 `sys.modules[name]` 暫時換成一個假模組（還原時本來沒有就拿掉）。

    「模組**不存在**」不走這條，走 `_NoModule`——`sys.modules[name] = None` 才是
    真的 ImportError，`pop`／`del` 只會讓下一次 import 載入**真的那一個**。
    """

    def __init__(self, name, module):
        self._name, self._value = name, module
        self._saved = _MISSING

    def __enter__(self):
        self._saved = sys.modules.get(self._name, _MISSING)
        sys.modules[self._name] = self._value
        return self

    def __exit__(self, *_exc):
        if self._saved is _MISSING:
            sys.modules.pop(self._name, None)
        else:
            sys.modules[self._name] = self._saved
        return False


@contextlib.contextmanager
def _posix_branch(fake_fcntl):
    """把 `_supervisor` 暫時拉進 POSIX 分支，yield 出記帳用的假 `os`。

    `fake_fcntl=None` ＝ 讓 `import fcntl` 丟 `ImportError`（走降級那條）。順手把
    `msvcrt.locking` 換成「一被呼叫就爆」，這樣萬一分支判斷壞掉、走回 Windows 那
    一半，是**當場失敗**而不是靠「flock 沒被呼叫」間接推論。
    """
    import _supervisor as sup

    fake_os = _RecordingPosixOs()
    with contextlib.ExitStack() as stack:
        stack.enter_context(_Swapped(sup, "os", fake_os))
        if fake_fcntl is None:
            stack.enter_context(_NoModule("fcntl"))
        else:
            stack.enter_context(_FakeModule("fcntl", fake_fcntl))
        if _REAL_MSVCRT is not None:
            stack.enter_context(
                _Swapped(_REAL_MSVCRT, "locking", _msvcrt_must_not_be_used))
        yield fake_os


def test_the_posix_branch_takes_a_non_blocking_exclusive_flock(tmp_path):
    """POSIX 那條路：`flock(fd, LOCK_EX | LOCK_NB)`，fd 就是剛 `os.open` 的那個。

    `LOCK_NB` 是這裡唯一真正致命的旗標。拿掉它 `flock` 會**阻塞**，於是第二個實例
    既不會回 `None` 也不會印任何東西——它就掛在那裡等第一個結束。開機自動啟動的
    那一份卡成這樣，使用者只會看到「bot 沒起來」，沒有任何線索。
    """
    fake_fcntl = _FakeFcntl()
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        try:
            assert lock is not None and not lock.degraded, (
                "POSIX 分支順利拿到鎖時要回一個正常的 InstanceLock")
            assert len(fake_os.opened) == 1, "應該只開一個 fd"
            assert fake_fcntl.calls == [
                (fake_os.opened[0],
                 _FakeFcntl.LOCK_EX | _FakeFcntl.LOCK_NB)
            ], (
                "flock 必須拿 os.open 回來的那個 fd，旗標必須是 "
                "LOCK_EX|LOCK_NB。少了 LOCK_NB 就是阻塞版：第二個實例不會被拒絕，"
                "會安靜地卡住。")
        finally:
            lock.release()


def test_the_posix_branch_maps_a_locked_file_to_none_and_closes_the_fd(tmp_path):
    """已被別的行程鎖住 ⇒ 回 `None`，而且**要把 fd 關掉**。

    `None` 是「已有實例」專用的答案，回 degraded 的殼就等於讓第二個實例照常啟動。
    fd 沒關掉的話，一支長命的啟動器每次退讓都漏一個 fd。
    """
    fake_fcntl = _FakeFcntl(error=OSError(11, "Resource temporarily unavailable"))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is None, (
            "flock 丟 OSError ＝ 已有另一個實例，必須回 None。回 InstanceLock "
            "（含 degraded 的殼）就是放第二個監督者進來。")
        assert len(fake_os.opened) == 1
        fd = fake_os.opened[0]
        assert fake_os.closed == [fd], "讓位那條路要把剛開的 fd 關掉"
        with pytest.raises(OSError):
            os.fstat(fd)        # 真的關了，不只是記了一筆


def test_the_posix_branch_without_fcntl_degrades_instead_of_refusing(tmp_path):
    """POSIX 上沒有 `fcntl`（極罕見）時往「照常啟動」倒，**不可以**回 `None`。

    與 `test_a_platform_without_file_locking_still_starts` 同一條規則，但走的是
    **另一行程式碼**：那支在這台機器上撞的是 `import msvcrt`，這支撞的是
    `import fcntl`。
    """
    with _posix_branch(None) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None, "不可回 None——那會被啟動器解讀成『已有實例』"
        assert lock.degraded is True, "鎖機制不可用時必須誠實標記成 degraded"
        # 這條路刻意**保留** fd（`InstanceLock(fd, ...)`），不像讓位那條當場關掉。
        assert len(fake_os.opened) == 1 and fake_os.closed == []
        lock.release()
        assert fake_os.closed == [fake_os.opened[0]]


# ---------------------------------------------------------------------------
# 「有人持有」與「這裡鎖不動」是兩件事，靠 errno 分（2026-09-08）
# ---------------------------------------------------------------------------
#
# 改之前 `acquire_single_instance_lock` 的 `except OSError` 一律 `return None`，
# 也就是把**所有**鎖失敗都講成「已經有另一個實例在執行」。那跟這個函式自己
# docstring 寫明的政策（判斷不出來就往「照常啟動」倒）**正好相反**，而且失敗形態
# 極難追：一個與併發完全無關的檔案系統問題（EBADF／EINVAL／不支援檔案鎖的網路
# 磁碟給的 ENOLCK）會讓啟動器**永遠拒絕啟動**，訊息還指著一個不存在的實例，甚至
# 附上「既有 pid」——而那份 pid 清單是另一支診斷函式掃出來的，跟鎖沒有關係。
#
# 本機實測（Windows 11 / CPython，2026-09-08）：
#
#   msvcrt.locking(fd, LK_NBLCK, 1) 對已鎖住的區段   → errno=13  EACCES
#   msvcrt.locking(fd, LK_LOCK,  1) 重試失敗          → errno=36  EDEADLOCK
#   msvcrt.locking(壞掉的 fd)                          → errno=9   EBADF
#   msvcrt.locking(fd, LK_NBLCK, -1)                   → errno=22  EINVAL
#
# 前兩個是「有人持有」，後兩個是「鎖不動」——**分得出來**，所以沒有理由混為一談。

_CONTENDED_ERRNOS = [
    ("EACCES", 13),        # Windows msvcrt：實測值
    ("EAGAIN", None),      # POSIX flock
    ("EWOULDBLOCK", None),  # POSIX flock（Windows 上跟 EAGAIN 不同值，見下）
    ("EDEADLK", None),     # ＝ EDEADLOCK(36)，阻塞版 msvcrt 的答案
]


@pytest.mark.parametrize(("name", "expected_value"), _CONTENDED_ERRNOS)
def test_a_contended_lock_errno_still_means_another_instance(
        name, expected_value, tmp_path):
    """代表「被別人持有」的那幾個 errno 必須照舊回 `None` 並關掉 fd。

    這是白名單**收得太緊**的方向：漏掉任何一個，真的有第二個實例時就會被放行，
    而那正是這把鎖存在的唯一理由。
    """
    import errno as errno_mod
    number = getattr(errno_mod, name, None)
    if number is None:
        pytest.skip(f"這個平台沒有 errno.{name}")
    if expected_value is not None:
        assert number == expected_value, (
            f"errno.{name} 在這台機器上是 {number}，不是實測記錄的 "
            f"{expected_value}——白名單的註解要跟著更新")

    fake_fcntl = _FakeFcntl(error=OSError(number, os.strerror(number)))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is None, (
            f"errno={number}（{name}）代表另一個實例正在跑，必須回 None。回 "
            "degraded 的殼就是放第二個監督者進來。")
        assert fake_os.closed == fake_os.opened, "讓位那條路要把 fd 關掉"


_UNDECIDABLE_ERRNOS = ["EBADF", "EINVAL", "ENOLCK", "ENOSYS", "EPERM", "EIO"]


@pytest.mark.parametrize("name", _UNDECIDABLE_ERRNOS)
def test_an_unknown_lock_errno_degrades_instead_of_claiming_another_instance(
        name, tmp_path):
    """不在白名單的 errno ＝「判斷不出來」⇒ degraded 的殼，**不是** `None`。

    **這條就是這次改動買到的全部價值。** 回 `None` 的話，一個與併發無關的檔案
    系統問題會被講成「已經有另一個實例在執行」，啟動器從此永遠拒絕啟動——而使用者
    看到的訊息指著一個不存在的實例，沒有任何線索指向真正的原因。
    """
    import errno as errno_mod
    number = getattr(errno_mod, name, None)
    if number is None:
        pytest.skip(f"這個平台沒有 errno.{name}")

    fake_fcntl = _FakeFcntl(error=OSError(number, os.strerror(number)))
    with _posix_branch(fake_fcntl) as fake_os:
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None, (
            f"errno={number}（{name}）代表鎖機制本身有問題，不代表有別的實例。"
            "回 None 會讓啟動器永遠拒絕啟動，而且錯誤訊息指向不存在的實例。")
        assert lock.degraded is True, "放行時必須誠實標記成 degraded"
        # 這條路刻意保留 fd（跟 `ImportError` 那條一致），`release()` 才關。
        assert fake_os.closed == []
        lock.release()


def test_an_oserror_without_an_errno_is_also_undecidable(tmp_path):
    """`OSError` 的 `errno` 可能是 `None`。那更是「判斷不出來」，不是「有人持有」。

    `None in frozenset_of_ints` 是 False，所以這條**自然**落在降級那邊；寫一支釘住
    是因為「順手把預設值改成回 None」看起來很無害。
    """
    fake_fcntl = _FakeFcntl(error=OSError("鎖呼叫壞了，沒有 errno"))
    with _posix_branch(fake_fcntl):
        lock = acquire_single_instance_lock(_lock_path(tmp_path))
        assert lock is not None and lock.degraded is True
        lock.release()


def test_the_contended_errno_list_covers_both_platforms():
    """白名單必須同時涵蓋 Windows 與 POSIX 的答案，而且不能假設兩者相等。

    `EWOULDBLOCK` 在 Linux 上就是 `EAGAIN`（都是 11），**但在 Windows 的 CPython
    上不是**：實測 `EAGAIN == 11`、`EWOULDBLOCK == 10035`（Winsock 的
    WSAEWOULDBLOCK）。只寫其中一個，就會在某一個平台上漏掉「已被持有」的答案。
    """
    import errno as errno_mod
    from _supervisor import _LOCK_HELD_ERRNOS  # noqa: SLF001

    for name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK"):
        number = getattr(errno_mod, name, None)
        if number is not None:
            assert number in _LOCK_HELD_ERRNOS, (
                f"errno.{name}（{number}）不在白名單裡——那個平台的「已被持有」"
                "會被誤判成「鎖壞了」，於是重複實例被放行。")
    for name in ("EBADF", "EINVAL", "ENOLCK"):
        number = getattr(errno_mod, name, None)
        if number is not None:
            assert number not in _LOCK_HELD_ERRNOS, (
                f"errno.{name}（{number}）不該在白名單裡——那是「鎖不動」，"
                "把它當成「有人持有」就會讓啟動器永遠拒絕啟動。")


# ---------------------------------------------------------------------------
# `degraded` 必須被講出來（2026-09-08）
# ---------------------------------------------------------------------------
#
# 在這之前，`degraded` 在正式程式碼裡**一處都沒有被讀過**——只有 `_supervisor.py`
# 的三個地方設定它，其餘全是測試在讀。也就是說「這台機器上的互斥保護沒有生效」
# 這件事，使用者永遠不會知道：啟動器照常啟動、log 一切正常，而重複實例會在幾天後
# 以完全不同的症狀出現（webrunner 那一側是兩個批次互相 nuclear sweep）。
#
# 無聲的降級跟沒有這把鎖是同一件事。上面那個 errno 白名單把「判斷不出來」從
# 「拒絕啟動」改成「照常啟動」，**這一節是那個改動的另一半**：放行可以，但不准
# 安靜地放行。


def _launcher_says_with_a_degraded_lock(launcher, tmp_path, monkeypatch):
    """讓啟動器拿到一個 degraded 的鎖，回收它 `say()` 出來的 `(訊息, err)`。

    `acquire_single_instance_lock` 直接換成回殼的替身——**不去真的製造一個鎖不動的
    檔案系統**。spawn 的入口一律換成會丟例外的攔截器（不可以回 rc：bot 那支的
    `while True:` 會把乾淨結束當成要重生，測試會掛住而不是紅掉）。
    """
    module = _load_launcher(launcher)
    work = tmp_path / launcher
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "LOCK_FILE", work / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG" if "webrunner" in launcher
                        else "BOT_LOG", work / "test.log")
    monkeypatch.setattr(sys, "argv", [launcher])

    from _supervisor import InstanceLock
    shell = InstanceLock(None, str(work / "test.lock"), degraded=True)
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: shell)

    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module, "say",
        lambda _log, message, **kwargs: said.append(
            (message, bool(kwargs.get("err", False)))))

    class _Spawned(Exception):
        pass

    for hook in ("_supervise", "stream_child"):
        if hasattr(module, hook):
            def _boom(*_a, **_k):
                raise _Spawned
            monkeypatch.setattr(module, hook, _boom)

    try:
        module.main()
    except _Spawned:
        pass                    # 走到 spawn 就夠了，這支不關心之後的事
    return said


def _degraded_lines(said):
    return [(message, err) for message, err in said
            if "單一實例保護" in message]


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_every_launcher_announces_a_degraded_lock(launcher, tmp_path,
                                                  monkeypatch):
    """互斥保護沒生效時，兩支啟動器都要出聲。

    這是 errno 白名單那個改動的配套：放行未知的鎖錯誤是刻意的取捨，但**放行不得
    是無聲的**。少了這一行，「這台機器上的鎖沒用」就變成只有讀原始碼才知道的事。
    """
    said = _launcher_says_with_a_degraded_lock(launcher, tmp_path, monkeypatch)
    lines = _degraded_lines(said)
    assert lines, (
        f"{launcher} 拿到 degraded 的鎖卻什麼都沒說。使用者會以為重複啟動被擋著，"
        "而實際上沒有。")

    for message, err in lines:
        # 降級**不是**錯誤，不要送到 stderr 讓它看起來像一次失敗。
        assert err is False, (
            f"{launcher} 把降級訊息當成錯誤送出（err=True）。它是降級不是失敗，"
            "混在錯誤裡會被當成雜訊略過。")
        # 訊息不得帶主機絕對路徑（鎖檔路徑正是最順手會被塞進去的東西）。
        assert ":\\" not in message and ":/" not in message, (
            f"{launcher} 的降級訊息帶了主機絕對路徑：{message!r}")
        assert str(tmp_path) not in message


def test_both_launchers_use_the_same_degraded_wording(tmp_path, monkeypatch):
    """兩支的措辭要一模一樣——這是「兩份實作」典型會各自漂移的地方。"""
    wordings = {}
    for launcher in _LAUNCHERS:
        said = _launcher_says_with_a_degraded_lock(launcher, tmp_path,
                                                   monkeypatch)
        lines = _degraded_lines(said)
        assert lines, f"{launcher} 沒有講出降級"
        wordings[launcher] = [message for message, _err in lines]

    first, second = (wordings[name] for name in _LAUNCHERS)
    assert first == second, (
        "兩支啟動器的降級訊息不一致。同一件事在兩個地方用兩種說法，讀 log 的人"
        f"會以為是兩種不同的狀況：\n  {first}\n  {second}")


# ---------------------------------------------------------------------------
# `other_launcher_pids`：診斷用的東西**永遠不該**把啟動器弄掛
# ---------------------------------------------------------------------------


def test_other_launcher_pids_without_psutil_returns_empty():
    """沒有 psutil 就回空 list——這只是給訊息用的診斷，決策是鎖的事。

    讓它往上丟 `ImportError` 的話，一個「順便講一下既有 pid」的功能會變成啟動器
    起不來的原因。
    """
    from _supervisor import other_launcher_pids
    with _NoModule("psutil"):
        assert other_launcher_pids("start_webrunner.py") == []


def test_other_launcher_pids_skips_a_process_it_cannot_parse():
    """單一筆行程資料壞掉時跳過它，**不要**把整份掃描結果丟掉。

    psutil 回來的東西不是我們控制的（`cmdline()` 在權限不足或行程剛死時可能回
    奇怪的值）。少認一筆只是訊息少一個 pid；整份掉光的話，使用者會看到「沒有其他
    實例」——而事實正好相反。
    """
    from _supervisor import other_launcher_pids
    procs = [(11, "python.exe", 12345, 1),          # cmdline 不是序列
             (12, "python.exe",
              ["py.exe", "D:/Work/Example/start_webrunner.py"], 1)]
    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=procs) == [12]


def test_other_launcher_pids_keeps_what_it_found_when_the_scan_blows_up():
    """掃描器本身掃到一半炸掉時，已經找到的照樣回報。

    這條與上一條刻意用**不同**的輸入：上一條的例外發生在單筆解析（內層），這一條
    發生在迭代器本身（外層）。用同一個輸入的話，兩層保護會互相遮蔽——刪掉任何一層
    測試都還是綠的。
    """
    from _supervisor import other_launcher_pids

    def _procs():
        yield (11, "python.exe",
               ["py.exe", "D:/Work/Example/start_webrunner.py"], 1)
        raise RuntimeError("psutil 掃到一半炸了")

    assert other_launcher_pids("start_webrunner.py", self_pid=99,
                               procs=_procs()) == [11]


# ---------------------------------------------------------------------------
# `trim_log` / `log_write` / `echo_line` / `pump_stream`：記錄檔壞掉不得傳染
# ---------------------------------------------------------------------------


def test_trim_log_stays_quiet_when_the_file_cannot_be_rewritten(tmp_path,
                                                                capsys):
    """修剪失敗只能是「這次不修剪」，不能是「監督者死掉」。

    `trim_log` 是本專案文件裡記載的原子寫入**例外**（它就地覆寫，因為這個檔案同時
    有兩個活著的 append 控制代碼），所以它沒有 `os.replace` 兜底——寫到一半失敗的
    可能性比別處高，這條路更該被驗過。

    順帶釘住「失敗要留下訊息」：整個吞掉的話，記錄檔會一路長下去而且一個字都不會
    說（那正是改成原子寫入之後會發生的事）。
    """
    import _supervisor as sup

    path = tmp_path / "readonly.log"
    path.write_text("keep-me\n" * 200, encoding="utf-8")
    before = path.read_bytes()
    os.chmod(path, 0o444)               # Windows 上＝設定唯讀屬性
    try:
        sup.trim_log(path, max_bytes=100, keep_bytes=50)
    finally:
        os.chmod(path, 0o644)
    assert path.read_bytes() == before, "寫不進去卻把檔案動了"
    assert "trim_log" in capsys.readouterr().err, (
        "修剪失敗必須留下一行診斷；靜默失效的話記錄檔會無限長大而沒有人知道。")


def test_log_write_survives_a_dead_log_handle(tmp_path):
    """記錄檔控制代碼壞掉時只是少一行，不能把抽水執行緒帶走。

    **關掉的串流丟的是 `ValueError` 不是 `OSError`**，兩個都要接——這也是
    `log_write` 的 `except` 寫成 `(OSError, ValueError)` 的原因。
    """
    import _supervisor as sup

    handle = (tmp_path / "closed.log").open("a", encoding="utf-8")
    handle.close()
    sup.log_write(handle, "掉進關掉的檔案\n")     # ValueError 不得外流

    class _FullDisk:
        def write(self, _text):
            raise OSError(28, "No space left on device")

    sup.log_write(_FullDisk(), "磁碟滿了\n")      # OSError 不得外流


def test_echo_line_falls_back_when_the_console_cannot_encode():
    """啟動器的 stdout 被重導向到 cp950 檔案時，編不出來的字要退成替換字元。

    **不要拿日文假名當測資**：實測 cp950（Big5）**編得出**假名
    （`"かな".encode("cp950")` ＝ `b"cf af cf ce"`），所以用假名的測試根本沒有走到
    退版路徑，卻看起來是綠的。這裡用 emoji——那是 Big5 真的沒有的。

    這條在這台機器上是真實情境而不是理論：`locale.getpreferredencoding(False)`
    是 cp950，而佇列內容裡有非中文字元。
    """
    import _supervisor as sup

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp950", errors="strict",
                              write_through=True)
    saved = sys.stdout
    sys.stdout = stream
    try:
        sup.echo_line("進度 \U0001F600 33/75\n")
    finally:
        sys.stdout = saved
    stream.flush()
    text = raw.getvalue().decode("cp950")
    assert "進度" in text and "33/75" in text, (
        f"退版寫法把整行弄丟了：{text!r}。編不出來的**只有那一個字元**，"
        f"其餘內容還是要看得到。")
    assert "?" in text, f"編不出來的字元沒有被替換掉：{text!r}"


class _EncodeThenFail:
    """第一次寫丟 `UnicodeEncodeError`，退版那次丟 `exc`。"""

    encoding = "cp950"

    def __init__(self, exc):
        self._exc = exc
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self.writes == 1:
            raise UnicodeEncodeError("cp950", text, 0, 1,
                                     "illegal multibyte sequence")
        raise self._exc

    def flush(self):
        return None


@pytest.mark.parametrize("exc", [
    OSError(28, "No space left on device"),      # 重導向的檔案所在磁碟滿了
    ValueError("I/O operation on closed file."),  # 管線讀端已經關掉
    LookupError("unknown encoding: bogus"),      # 串流謊報自己的編碼
])
def test_a_console_that_breaks_during_the_fallback_never_escapes(exc):
    """**真缺陷（2026-09-07 修）**：退版寫入原本完全沒有被保護。

    退版寫法原本住在 `except UnicodeEncodeError:` 區塊裡，而下面掛著
    `except OSError: pass`——但**從 `except` 區塊裡丟出來的例外不會被同一個 `try`
    的其他 `except` 接住**。實測：第一次寫丟 `UnicodeEncodeError`、退版那次丟
    `OSError(28)`，那個 `OSError` 直接穿出 `echo_line`。

    後果不是掉一行日誌：`echo_line` 跑在抽水執行緒上，例外會終止整條抽水迴圈，
    子行程接著被自己塞滿的管線卡死——監督者還活著、批次卻不動了，而且什麼都不會
    說。這支測試就是釘住「一行日誌絕對不能弄死監督者」。
    """
    import _supervisor as sup

    stream = _EncodeThenFail(exc)
    saved = sys.stdout
    sys.stdout = stream
    try:
        sup.echo_line("進度 \U0001F600\n")   # 不得丟例外
    finally:
        sys.stdout = saved
    assert stream.writes == 2, (
        f"退版寫入根本沒有被嘗試（writes={stream.writes}）")


def test_a_broken_console_does_not_stop_the_pump():
    """主控台寫壞掉時，抽水**必須繼續**——這是那個死結的迴歸測試。

    `pump_stream` 的 `except (OSError, ValueError)` 包的是整個迴圈，所以只要
    `echo_line` 丟得出例外，主控台壞一次就等於整條抽水停掉。實測（修之前）：3 行
    只抽到 1 行。子行程接下來會把 ~64 KB 的管線緩衝區塞滿，然後**永遠卡在下一個
    print**——比「沒有記錄檔」糟得多，因為批次會整個停住而不是掉一份 log。
    """
    import _supervisor as sup

    class _AlwaysClosed:
        encoding = "cp950"

        def write(self, _text):
            raise ValueError("I/O operation on closed file.")

        def flush(self):
            return None

    lines = ["第一行\n", "第二行\n", "第三行\n"]
    log = io.StringIO()
    saved = sys.stdout
    sys.stdout = _AlwaysClosed()
    try:
        sup.pump_stream(iter(lines), log)
    finally:
        sys.stdout = saved
    assert log.getvalue().count("行") == 3, (
        f"主控台壞掉把抽水一起帶走了，只抽到 {log.getvalue()!r}。"
        "子行程接下來會被自己塞滿的管線卡死。")


def test_pump_stream_tolerates_no_stream_at_all():
    """`proc.stdout` 是 `None`（呼叫端沒給 PIPE）時安靜收工。"""
    import _supervisor as sup
    sup.pump_stream(None, None)


def test_pump_stream_stops_quietly_when_the_pipe_dies():
    """管線中途壞掉：已經讀到的要留住，而且不得往上丟。

    抽水跑在 daemon 執行緒上，它丟出來的例外只會印一段沒有上下文的
    `Exception in thread`，然後 `stream_child` 在 `proc.wait()` 那裡繼續等——
    看起來像當掉，實際上是抽水已經死了。
    """
    import _supervisor as sup

    class _DyingPipe:
        def __iter__(self):
            return self

        def __next__(self):
            if not hasattr(self, "_done"):
                self._done = True
                return "壞掉之前這行要留住\n"
            raise OSError(22, "The handle is invalid")

    log = io.StringIO()
    sup.pump_stream(_DyingPipe(), log)          # 不得丟例外
    assert "壞掉之前這行要留住" in log.getvalue()


# ---------------------------------------------------------------------------
# `stream_child`：子行程已經起來之後的每一條錯誤路徑
# ---------------------------------------------------------------------------


class _ProcWrapper:
    """包住真的 `Popen`，只換掉要驗的那一個行為，其餘照實委派。"""

    def __init__(self, proc):
        self._proc = proc

    def __getattr__(self, name):
        if name == "_proc":                     # 防止 `_proc` 還沒設好時無限遞迴
            raise AttributeError(name)
        return getattr(self._proc, name)


def _popen_shim(monkeypatch, wrap):
    """把 `_supervisor` 命名空間裡的 `subprocess` 換成薄殼：照樣起真的子行程，
    只是回傳前先讓 `wrap` 動手腳。

    只換 `_supervisor.subprocess` 這個**名字**，不動 stdlib 模組本身——後者是全
    行程生效的，剛好有別的執行緒在 spawn 就會一起中招。
    """
    import _supervisor as sup

    real = subprocess

    class _Shim:
        PIPE = real.PIPE
        STDOUT = real.STDOUT
        TimeoutExpired = real.TimeoutExpired

        @staticmethod
        def Popen(*args, **kwargs):             # noqa: N802  # 對齊 stdlib 命名
            return wrap(real.Popen(*args, **kwargs))

    monkeypatch.setattr(sup, "subprocess", _Shim)


_SLEEPY_CHILD = (
    "import sys, time\n"
    "print('CHILD-UP')\n"
    "sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def test_ctrl_c_reaps_the_child_instead_of_orphaning_it(tmp_path, monkeypatch):
    """Ctrl+C：例外要往上送（啟動器靠它收工），但子行程必須先被收乾淨。

    直接把 `KeyboardInterrupt` 往上拋就走人的話會留下孤兒——webrunner 那一側連帶
    留下整棵 Chrome，下一次啟動就變成兩套堆疊搶同一份 `.chrome_profile/`。
    這裡用真的子行程 ＋ 真的管線，只把 `proc.wait()` 換成丟 `KeyboardInterrupt`
    （測試裡沒有辦法對自己的主控台群組送真的 Ctrl+C 而不把 pytest 一起帶走）。
    """
    import _supervisor as sup

    log_path = tmp_path / "ki.log"

    class _InterruptOnWait(_ProcWrapper):
        def __init__(self, proc):
            super().__init__(proc)
            self.interrupted = False

        def wait(self, timeout=None):
            # 只攔 `stream_child` 那一次（沒有 timeout）；`reap_child` 帶著
            # timeout 的那幾次要照實跑，否則驗到的就不是收屍流程本身。
            if timeout is None and not self.interrupted:
                # 先等子行程真的開口，再送 Ctrl+C。原本是一開始就送，於是這支
                # 默默假設「子行程在 0.5 秒寬限期內就印得出第一行」——2026-09-22
                # 從 IDE 啟動的環境帶著一個 `sitecustomize`，每個 Python 子行程
                # 要 1.2 秒才起得來，這支就在兩個直譯器上一起紅了，而程式碼一個字
                # 都沒動。要驗的是「Ctrl+C 之前說過的話不會不見」，前提是它真的
                # 說過；等待有上限，逾時就照原樣送出，讓下面的斷言講出原因。
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    try:
                        if "CHILD-UP" in log_path.read_text(
                                encoding="utf-8", errors="replace"):
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                self.interrupted = True
                raise KeyboardInterrupt
            return self._proc.wait(timeout=timeout)

    holder = {}

    def _wrap(proc):
        holder["proc"] = _InterruptOnWait(proc)
        return holder["proc"]

    _popen_shim(monkeypatch, _wrap)

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as handle:
            with pytest.raises(KeyboardInterrupt):
                sup.stream_child([sys.executable, "-u", str(script)], handle,
                                 cwd=str(tmp_path), pump_name="ki-pump",
                                 grace_sec=0.5, kill_sec=10.0)
    finally:
        proc = holder.get("proc")
        if proc is not None and proc.poll() is None:   # 保險：絕不留孤兒
            proc.kill()
            proc.wait(timeout=30)

    proc = holder["proc"]
    assert proc.poll() is not None, (
        "Ctrl+C 之後子行程還活著——這正是那些握著整棵 Chrome 的孤兒的來源。")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert "CHILD-UP" in log, (
        "Ctrl+C 之前子行程說過的話不見了；那段輸出正是事後要查的東西。")
    assert "terminating" in log, (
        "子行程沒在寬限期內收工，卻沒有走到 terminate；收屍是先禮後兵。")
    assert not [t for t in threading.enumerate() if t.name == "ki-pump"], (
        "抽水執行緒沒有收掉。")


def test_a_stdout_that_refuses_to_close_does_not_swallow_the_exit_code(
        tmp_path, monkeypatch):
    """收尾時 `proc.stdout.close()` 失敗，不得把已經拿到的 rc 換成例外。

    這一行住在 `finally` 裡，所以它丟出來的例外會**取代** `return rc`——監督者拿
    不到子行程的退出碼，rapid-fail giveup 與 `child_exit_is_fatal` 兩條判斷同時
    失效，而真正的原因（子行程為什麼結束）已經被蓋掉了。
    """
    import _supervisor as sup

    class _CloseFails:
        def __init__(self, stream):
            self._stream = stream
            self.attempts = 0

        def __iter__(self):
            return iter(self._stream)

        def close(self):
            self.attempts += 1
            self._stream.close()        # 真的關掉，不然 fd 會漏
            raise OSError(5, "Input/output error")

    class _StdoutCloseFails(_ProcWrapper):
        def __init__(self, proc):
            super().__init__(proc)
            self._stdout = _CloseFails(proc.stdout)

        @property
        def stdout(self):
            return self._stdout

    holder = {}

    def _wrap(proc):
        holder["proc"] = _StdoutCloseFails(proc)
        return holder["proc"]

    _popen_shim(monkeypatch, _wrap)

    script = tmp_path / "quick.py"
    script.write_text("import sys\nprint('bye')\nsys.exit(9)\n",
                      encoding="utf-8")
    console = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(console):
        rc = sup.stream_child([sys.executable, "-u", str(script)], None,
                              cwd=str(tmp_path), pump_name="close-pump")
    assert rc == 9, f"收尾的 close 失敗把 rc 吃掉了（rc={rc}）"
    assert holder["proc"].stdout.attempts == 1


def test_the_spawn_hook_runs_before_the_child_is_waited_on(tmp_path):
    """`on_spawn` 要在 `proc.wait()` **之前**跑到，而且拿得到真的 pid。

    `start_webrunner._on_spawn` 在那裡寫 `webrunner.pid` 並放掉 Chrome 槽；晚一步
    就會出現「槽空了、pid 還沒寫」的空窗，驗證端剛好在那一瞬間取槽就會判定沒人在
    跑、開出第二個 Chrome stack。
    """
    seen = []
    rc, log, _console = _run_child(tmp_path, "print('hi')\n",
                                   on_spawn=lambda proc: seen.append(proc.pid))
    assert rc == 0
    assert seen and isinstance(seen[0], int), "on_spawn 沒被呼叫到"
    assert "hi" in log


def test_a_failing_spawn_hook_does_not_leave_an_orphan(tmp_path):
    """**真缺陷（2026-09-07 修）**：spawn hook 失敗會留下沒人收的子行程。

    `on_spawn` 做的是真的 I/O——`start_webrunner._on_spawn` 寫 `webrunner.pid`
    ——磁碟滿了或權限不對就丟 `OSError`。修之前那個例外直接往上送，而子行程**已經
    起來了**：stdout 是 PIPE、沒有人抽、也沒有人 `wait` 它。它下一次 print 就卡死
    在滿掉的管線裡，而它手上握著整棵 Chrome。監督者自己死掉、批次還在那裡卡著
    不動，是最難查的那種收場。

    例外照樣要往上送（寫不進 pid 檔是嚴重的事，吞掉會讓驗證端闖進正在跑的批次），
    但**送出去之前要先收屍**。
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    holder = {}

    def _boom(proc):
        holder["proc"] = proc
        raise OSError(28, "No space left on device")

    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError):
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), on_spawn=_boom,
                                 pump_name="boom-pump", kill_sec=10.0)
    finally:
        proc = holder.get("proc")
        if proc is not None and proc.poll() is None:   # 保險：絕不留孤兒
            proc.kill()
            proc.wait(timeout=30)
            pytest.fail(
                "spawn hook 失敗之後子行程還活著。它的 stdout 是沒有人抽的 "
                "PIPE，下一個 print 就會卡死，而它握著整棵 Chrome。")

    assert holder["proc"].returncode is not None, (
        "子行程沒有被收屍——`stream_child` 起了它就有責任把它收掉。")


def test_a_child_that_survives_reaping_does_not_wedge_the_shutdown(
        tmp_path, monkeypatch):
    """收屍失敗、子行程還活著時，收尾那一行不得把監督者永久卡住。

    實測（2026-09-07，本機 Windows 11 / CPython 3.14）：抽水執行緒卡在 `read()`
    的時候呼叫 `proc.stdout.close()`，**不會**丟例外、也不會把串流從抽水手上抽走
    ——它去搶同一把鎖，於是一路擋到那次 read 回來為止，量到 **19.05 秒**，正好是
    子行程還活著的那段時間。

    正常路徑碰不到（`proc.wait()` 回來就代表子行程死了、管線 EOF、抽水立刻結束），
    碰得到的是 Ctrl+C 之後 `reap_child` 自己失敗那條。那時候的下場最難查：啟動器
    永遠停在收工的最後一行，**而且還握著單一實例鎖**，於是誰也重啟不了，主控台上
    什麼訊息都沒有——會印訊息的那個東西就是卡住的那個。
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    real_procs = []

    class _SurvivesReaping(_ProcWrapper):
        """Ctrl+C 之後怎麼收都收不掉的子行程（terminate／kill 都失敗）。"""

        def __init__(self, proc):
            super().__init__(proc)
            self.interrupted = False

        def wait(self, timeout=None):
            if timeout is None and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def terminate(self):
            raise OSError(5, "Access is denied")

        def kill(self):
            raise OSError(5, "Access is denied")

    def _wrap(proc):
        real_procs.append(proc)
        return _SurvivesReaping(proc)

    _popen_shim(monkeypatch, _wrap)
    # 把 join 的等待縮短，讓「有沒有卡住」的差距是 0.5 秒 vs 子行程的整段壽命，
    # 而不是兩個相近的數字——時間斷言只有在差距夠大時才不會變成隨機紅的守門。
    monkeypatch.setattr(sup, "_LOG_PUMP_JOIN_SEC", 0.5)

    console = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError):
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), pump_name="wedge-pump",
                                 grace_sec=0.2, kill_sec=0.2)
        elapsed = time.monotonic() - started
    finally:
        for proc in real_procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
    assert elapsed < 8.0, (
        f"收工卡了 {elapsed:.1f} 秒（子行程活 30 秒）。抽水還在讀的時候關串流會"
        "一路擋到那次 read 回來為止——監督者會永久停在這裡，而且還握著單一實例"
        "鎖，誰也重啟不了。")


def test_a_failing_reap_does_not_mask_why_the_spawn_hook_failed(tmp_path,
                                                                monkeypatch):
    """收屍自己也失敗時，往上送的必須還是**原本**那個例外。

    這是錯誤路徑上的經典壞法：清理程式碼丟出自己的例外，把真正的原因蓋掉。這裡
    蓋掉的會是「pid 檔寫不進去」（要處理的事），換成「terminate 失敗」（處理不完
    的表面現象），而讀 log 的人看不到前者存在過。
    """
    import contextlib
    import _supervisor as sup

    script = tmp_path / "sleepy.py"
    script.write_text(_SLEEPY_CHILD, encoding="utf-8")
    real_procs = []

    class _Unreapable(_ProcWrapper):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout)

        def terminate(self):
            raise OSError(5, "Access is denied")

        def kill(self):
            raise OSError(5, "Access is denied")

    def _wrap(proc):
        real_procs.append(proc)
        return _Unreapable(proc)

    _popen_shim(monkeypatch, _wrap)

    def _boom(_proc):
        raise OSError(28, "No space left on device")

    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            with pytest.raises(OSError) as info:
                sup.stream_child([sys.executable, "-u", str(script)], None,
                                 cwd=str(tmp_path), on_spawn=_boom,
                                 pump_name="mask-pump", kill_sec=1.0)
        assert info.value.errno == 28, (
            f"往上送的是收屍失敗的例外（errno={info.value.errno}），原本那個"
            "「pid 檔寫不進去」被蓋掉了。")
    finally:
        for proc in real_procs:                 # 這支測試刻意讓收屍失敗，自己收
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


# ---------------------------------------------------------------------------
# 讀不出 `webrunner.pid` 時，啟動器要走保守的那一邊（2026-09-07）
#
# `start_webrunner._live_webrunner_pid` 原本把「檔案不存在」與「檔案在、但讀不出
# 來」都回成 `None`，而 `None` 在呼叫端的意思是**「沒有批次在跑，可以起一個」**
# ——判不出來卻走了樂觀的那一邊。代價是同一台機器上跑起第二個批次：兩個 webrunner
# 搶同一份 `.chrome_profile/`、各自 nuclear sweep 把對方的 Chrome 殺掉、從同一組
# `todo_*.md` 重複取件，而兩邊的紀錄看起來都正常。CLAUDE.md 的 Windows PID 存活
# 硬規則對這一類判斷寫得很明白：問「我該不該不要啟動／讓位？」的地方，判不出來
# 就要走保守的那一邊。姊妹函式 `verify_browser._live_webrunner_pid` 同日修成
# `(pid, decided)`，這裡照同一個形狀。
#
# 順帶修掉的第二個洞：`UnicodeDecodeError` 是 `ValueError` 的子類、**不是**
# `OSError`，所以 `except (FileNotFoundError, OSError)` 接不到它——pid 檔內容不是
# 合法 UTF-8 時，兩支讀檔函式都會直接炸穿。`_clear_pid_if_ours` 的 docstring 還
# 明寫著「永不 raise」，而它是在 `finally` 裡被呼叫的。
#
# 這組測試**永遠不碰正式的 `webrunner.pid`**（本機常有跑了幾十小時的批次在用它），
# 一律 monkeypatch 到 `tmp_path`；`_chrome_slot` 也一律換成替身，免得去動 repo root
# 那把真的槽鎖。
# ---------------------------------------------------------------------------

WEBRUNNER_LAUNCHER = "start_webrunner.py"


class _FakeSlot:
    """夠像 `_chrome_slot` 的替身：一定拿得到槽、記錄釋放、pid 存活可控。

    換掉它是**安全需求**而不是方便：真的 `_chrome_slot` 會去動 repo root 的
    `chrome_slot.lock`，而正式批次正在用那把槽。
    """

    def __init__(self, *, pid_alive=True):
        self.released = []
        self._alive = pid_alive

    def acquire(self, owner, *, timeout=0.0, label=""):
        return True

    def release(self, owner):
        self.released.append(owner)

    def _pid_alive(self, pid):
        return self._alive


@pytest.fixture(autouse=True)
def _no_real_network_probe(monkeypatch):
    """這個檔案裡的 `_supervise` 不准碰真的網路。

    子行程以非零結束時，啟動器會先問「主機連得上網路嗎」，判成斷網就不計入放棄門檻、改成
    **無限期**等網路回來。原本這裡沒有替身，於是每一支讓子行程失敗的測試都真的對外連線：
    網路一抖、或主機忙到探測逾時，快速失敗就不被計入（2026-09-23 滿載時實際紅過一支），
    真的斷網的話整個套件會卡在那裡。斷網那條路另有 `test_batch_recovery` 專門測。
    換的是啟動器實際拿到的那個模組物件（`axiomatic._connectivity`）。
    """
    from axiomatic import _connectivity as launcher_connectivity  # noqa: PLC0415

    def _no_wait(*_args, **_kwargs):
        raise AssertionError("啟動器以為斷網、開始等網路——這個檔案的測試不該走到這裡")

    monkeypatch.setattr(launcher_connectivity, "is_online", lambda *_a, **_k: True)
    monkeypatch.setattr(launcher_connectivity, "wait_until_online", _no_wait)


def test_the_launcher_in_this_file_never_probes_the_real_network(monkeypatch, tmp_path):
    """上面那個替身真的接到啟動器用的模組：底層連線全部失敗時，啟動器仍然回「有網路」。"""
    import socket  # noqa: PLC0415

    def _refuse(*_args, **_kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    module, _slot = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    assert module._network_is_up() is True


def _launcher_with_pid_file(monkeypatch, tmp_path, content, *, pid_alive=True):
    """載入啟動器並把 `WEBRUNNER_PID_FILE` 指到 `tmp_path`。

    `content` 是 bytes（刻意不是 str——要測得到「不是合法 UTF-8」那條路），
    `None` ＝ 不建檔。回 `(module, fake_slot)`。
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    path = tmp_path / "webrunner.pid"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", path)
    slot = _FakeSlot(pid_alive=pid_alive)
    monkeypatch.setattr(module, "_chrome_slot", slot)
    return module, slot


def test_a_missing_pid_file_means_there_is_really_no_batch(monkeypatch,
                                                           tmp_path):
    """檔案不存在是**判定得出來**的答案：真的沒有批次，可以起一個。"""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    assert module._live_webrunner_pid() == (None, True)


def test_a_live_pid_is_reported_as_a_running_batch(monkeypatch, tmp_path):
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"4242")
    assert module._live_webrunner_pid() == (4242, True)


def test_a_dead_pid_reads_as_no_batch(monkeypatch, tmp_path):
    """死掉的 pid 是**判定得出來**的「沒有批次」——被硬殺時會留下這種檔案，
    這條不能跟「讀不出來」混在一起，否則一次硬殺就讓啟動器再也起不來。"""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"999999",
                                        pid_alive=False)
    assert module._live_webrunner_pid() == (None, True)


def test_an_undecodable_pid_file_is_undecidable_not_empty(monkeypatch,
                                                          tmp_path):
    """內容不是合法 UTF-8 時，不得被當成「沒有批次在跑」。

    兩件事一起釘：這個函式**不得 raise**（`UnicodeDecodeError` 不是 `OSError`），
    而且回的必須是「判不出來」而不是「沒有批次」——後者會讓啟動器在正式批次旁邊
    再起一個 webrunner。
    """
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        b"\xff\xfe\x00\x80")
    pid, decided = module._live_webrunner_pid()      # 不得 raise
    assert pid is None
    assert decided is False, (
        "讀不出內容卻回報「判定得出來、沒有批次」——啟動器會據此再起一個 webrunner")


def test_a_non_numeric_pid_file_is_undecidable(monkeypatch, tmp_path):
    """讀得出來但不是數字（含空字串），同樣是判不出來，不是「沒有批次」。"""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        "不是數字".encode("utf-8"))
    assert module._live_webrunner_pid() == (None, False)


def test_an_unreadable_pid_file_is_undecidable(monkeypatch, tmp_path):
    """讀取本身丟 `OSError`（權限、檔案被鎖）也要走保守的那一邊。"""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"123")
    real = tmp_path / "webrunner.pid"

    class _Locked:
        def exists(self):
            return True

        def read_text(self, *_a, **_k):
            raise PermissionError("locked")

    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", _Locked())
    assert module._live_webrunner_pid() == (None, False)
    assert real.exists()                              # 沒有動到真的檔案


def _run_one_round(monkeypatch, tmp_path, content, *, pid_alive=True, rc=0):
    """在替身槽底下跑一輪 `_supervise`，回 `(module, exit_code, 有沒有 spawn, slot)`。

    `stream_child` 換成間諜，所以**這支測試永遠不會真的起出一個 webrunner**——
    webrunner 一啟動就無條件 nuclear sweep 掉全機 Chrome，測一個安全機制不該冒它
    要防的那個險。
    """
    module, slot = _launcher_with_pid_file(monkeypatch, tmp_path, content,
                                           pid_alive=pid_alive)
    spawned = []

    def _spy(cmd, _log, **_kwargs):
        spawned.append(cmd)
        return rc

    monkeypatch.setattr(module, "stream_child", _spy)
    code = module._supervise(
        ["python", "-u", "webrunner.py"], "selenium", None,
        backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
        rapid_threshold_sec=30.0, rapid_giveup=3, zero_progress_giveup=3)
    return module, code, bool(spawned), slot


def test_an_undecidable_pid_file_never_spawns_a_webrunner(monkeypatch,
                                                          tmp_path):
    """**這組修改真正要保證的性質**：判不出來就不准啟動。

    刻意用行為測試而不是 AST 掃描。姊妹函式那邊的變異測試當場證明 AST 守門太弱：
    只檢查「第二個回傳值有被接下來、那個名字有出現過」的話，把 `if not decided:`
    改成 `if False and not decided:` 之後名字**仍然出現**，守門照樣全綠，而行為
    已經退回「判不出來就啟動」。**能被恆假條件繞過的性質，只能用行為釘。**
    """
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path,
                                                 b"\xff\xfe\x00\x80")
    assert not spawned, "pid 檔讀不出來，卻還是起了一個 webrunner"
    assert code == 1, f"讓位應該回 rc=1，實際回 {code}"
    assert slot.released == [module.SLOT_OWNER], (
        f"沒有把 Chrome 槽放掉（或放了不只一次）：{slot.released}——"
        "早退路徑漏放的話，槽會一直被佔到 staleness 逾時才回收。")


def test_a_live_batch_never_spawns_a_second_webrunner(monkeypatch, tmp_path):
    """既有的讓位路徑也用行為釘一次（原本一支測試都沒有）。"""
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path,
                                                 b"4242")
    assert not spawned, "已經有批次在跑，卻還是起了第二個 webrunner"
    assert code == 1
    assert slot.released == [module.SLOT_OWNER]


def test_a_clean_machine_really_does_spawn(monkeypatch, tmp_path):
    """反方向，缺了會很糟：只釘「判不出來不准啟動」的話，把函式改成**永遠**回
    「不要啟動」也會全綠——那樣啟動器再也起不來，比原本的缺陷更糟，而且症狀是
    「按了沒反應」，沒人會往這裡找。"""
    module, code, spawned, slot = _run_one_round(monkeypatch, tmp_path, None,
                                                 rc=0)
    assert spawned, "沒有批次在跑，卻沒有起 webrunner"
    assert code == 0
    assert slot.released == [module.SLOT_OWNER]


def test_a_dead_pid_file_still_lets_the_launcher_start(monkeypatch, tmp_path):
    """硬殺之後留下的 pid 檔不得把啟動器擋死——那是正常的復原情境。"""
    _module, code, spawned, _slot = _run_one_round(monkeypatch, tmp_path,
                                                   b"999999", pid_alive=False)
    assert spawned, "殘留的死 pid 檔把啟動器擋住了（硬殺之後就再也起不來）"
    assert code == 0


def test_clearing_the_pid_file_only_touches_our_own_pid(monkeypatch, tmp_path):
    """bot 也寫同一個檔；刪掉別人的存活訊號＝驗證端會闖進正在跑的批次。"""
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path, b"4242")
    path = tmp_path / "webrunner.pid"
    module._clear_pid_if_ours(999)
    assert path.exists(), "刪掉了別人寫的 pid"
    module._clear_pid_if_ours(4242)
    assert not path.exists(), "自己寫的 pid 沒有被收回"


def test_clearing_the_pid_file_never_raises_on_an_undecodable_file(monkeypatch,
                                                                   tmp_path):
    """`_clear_pid_if_ours` 的 docstring 寫著「永不 raise」，那要是真的。

    它在 `finally` 裡被呼叫：從這裡丟出去的例外會蓋掉子行程真正的結束原因，
    而讀紀錄的人看不到前者存在過。方向本來就對（讀不出來就不刪），缺的只是把
    `UnicodeDecodeError` 接進來。
    """
    module, _ = _launcher_with_pid_file(monkeypatch, tmp_path,
                                        b"\xff\xfe\x00\x80")
    module._clear_pid_if_ours(4242)                   # 不得 raise
    assert (tmp_path / "webrunner.pid").exists(), "讀不出內容卻把檔案刪了"


# ---------------------------------------------------------------------------
# 兩支啟動器量「子行程活了多久」都要用單調時鐘（2026-09-07）
#
# `start_webrunner` 的 `alive_for` 與 `start_discord_bot` 的 `ran_for` 原本都是
# `time.time() - start`。兩個都是純粹的**行程內間隔**——值不寫檔、不跟任何檔案
# mtime 比對——所以判準（見 `_chrome_slot.acquire` 的 docstring）說得很清楚：該用
# `time.monotonic()`。
#
# 牆鐘會被 NTP 的 step 修正、手動改時鐘、虛擬機快照還原**跳動**（換時區與日光
# 節約時間不會，`time.time()` 回的是 UTC epoch 秒）。兩個方向都會壞：
#
# * 往回撥 → 間隔變小甚至變負 → 跑得好好的子行程被判成 rapid fail →
#   webrunner 那支**提早放棄**，bot 那支把退避一路養大。
# * 往前撥 → 看起來活很久 → rapid-fail 計數被重置、退避被重置 →
#   監督者**無限重生**一個真的壞掉的子行程。
#
# 無人值守的機器上第二種特別糟：它把「放棄並留下紀錄」變成「安靜地一直重試」。
#
# 這一組**全部是行為測試**：假時鐘讓兩個時鐘分岔，然後斷言監督者的判定沒有跟著
# 牆鐘跑。單看原始碼有沒有寫 `monotonic` 是不夠的（同一份教訓見上一節的 M4/M5）。
# ---------------------------------------------------------------------------


class _Clock:
    """假時鐘：`monotonic` 只會往前走，`time`（牆鐘）可以被單獨跳。

    **假時鐘一定要會走，不可以釘成常數。** 被量的那個間隔是「現在 − 一開始讀到
    的值」，兩邊都釘死的話間隔永遠是 0，測試會為了錯的理由變綠（或變紅），而不是
    因為它要驗的那件事。`sleep` 也推時鐘，否則退避那段時間在假時鐘裡等於沒發生。
    """

    def __init__(self, *, wall=1_700_000_000.0, mono=1_000.0):
        self.wall = wall
        self.mono = mono
        self.slept = []

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        """時間真的過去了——兩個時鐘一起走（正常情況）。"""
        self.wall += seconds
        self.mono += seconds

    def step_wall(self, seconds):
        """**只動牆鐘**：NTP step 修正／有人改了時鐘／虛擬機快照還原。"""
        self.wall += seconds


class _NoMoreRounds(Exception):
    """腳本跑完了，用它把監督者的無限迴圈拆掉。

    刻意丟例外而不是回一個 rc：回 rc 的話兩支監督者都會把它當成「要再重生一次」，
    於是測試不是紅掉而是**掛住**（這個檔案上面那支鎖的測試就實際踩過）。
    """


def _clocked_child(clock, rounds, spawns):
    """做一個假的 `stream_child`：照 `rounds` 推時鐘、回 rc。

    `rounds` 的每一筆是 `(這一輪真的過了幾秒, 牆鐘額外跳幾秒, rc)`。
    """
    script = list(rounds)

    def _spy(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if not script:
            raise _NoMoreRounds
        ran_for, wall_jump, rc = script.pop(0)
        clock.advance(ran_for)
        clock.step_wall(wall_jump)
        return rc

    return _spy


def _supervise_with_clock(monkeypatch, tmp_path, rounds, *,
                          rapid_giveup=1, zero_progress_giveup=99):
    """在假時鐘底下跑 `start_webrunner._supervise`，回 `(rc, clock, spawn 次數)`。

    `rc is None` 代表腳本用完、迴圈還想再轉一輪（＝監督者**沒有**放棄）。
    """
    module, _slot = _launcher_with_pid_file(monkeypatch, tmp_path, None)
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []
    monkeypatch.setattr(module, "stream_child",
                        _clocked_child(clock, rounds, spawns))
    try:
        rc = module._supervise(
            ["python", "-u", "webrunner.py"], "selenium", None,
            backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
            rapid_threshold_sec=30.0, rapid_giveup=rapid_giveup,
            zero_progress_giveup=zero_progress_giveup)
    except _NoMoreRounds:
        rc = None
    return rc, clock, len(spawns)


def test_the_webrunner_launcher_still_judges_a_run_by_its_real_length(
        monkeypatch, tmp_path):
    """沒有任何時鐘跳動時的基準：長跑算健康、短跑算 rapid fail。

    少了這一支，把 `alive_for` 寫死成任何一個常數都可能讓下面兩支「因為錯的理由」
    變綠。
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(120.0, 0.0, 1), (0.0, 0.0, 0)])
    assert (rc, spawns) == (0, 2), "活了 120 秒卻沒被算成健康"

    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1)])
    assert (rc, spawns) == (1, 1), "2 秒就崩潰卻沒被算成 rapid fail"


def test_a_backwards_clock_step_does_not_make_a_healthy_run_look_rapid(
        monkeypatch, tmp_path):
    """牆鐘被往回撥時，一次健康的執行不得被誤判成 rapid fail。

    子行程真的活了 120 秒（> `healthy_threshold_sec`），但期間牆鐘被往回撥 10 分鐘。
    用 `time.time()` 量的話 `alive_for` 會變成 −480 秒——比任何門檻都小——於是
    `rapid_fail_giveup_count=1` 當場觸發，監督者**在第一輪就放棄**，而那個子行程
    其實好得很。
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(120.0, -600.0, 1), (0.0, 0.0, 0)])
    assert spawns == 2, (
        "牆鐘往回撥之後監督者就放棄了——一次健康的執行被算成 rapid fail。"
        "間隔要用 `time.monotonic()` 量，它不受時鐘調整影響。")
    assert rc == 0


def test_a_forwards_clock_step_does_not_reset_the_rapid_fail_counter(
        monkeypatch, tmp_path):
    """牆鐘被往前撥時，一次真正的快速崩潰仍然要被算進 rapid-fail。

    **這是兩個方向裡比較危險的那一個**：子行程 2 秒就死，但牆鐘往前跳了 10 分鐘，
    用 `time.time()` 量會得到 602 秒 ≥ `healthy_threshold_sec` → 判成健康 →
    計數歸零 → 監督者**無限重生**一個真的壞掉的子行程。無人值守的機器上，這會把
    「放棄並留下紀錄」變成「安靜地一直重試」。
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, [(2.0, 600.0, 1)])
    assert spawns == 1, (
        "牆鐘往前撥之後，一次 2 秒就死的崩潰被當成健康執行，監督者又重生了一輪。")
    assert rc == 1


def _bot_launcher_with_clock(monkeypatch, tmp_path, rounds):
    """在假時鐘底下跑 `start_discord_bot.main()`，回 `(每一輪睡了幾秒, spawn 次數)`。

    這支啟動器的迴圈是 `while True` 且**沒有**放棄機制（刻意的：網路斷一下不該
    讓 bot 永久離線），所以可觀察的結果是**退避序列**——健康的一輪會把它打回 5 秒，
    崩潰的一輪會讓它翻倍。
    """
    module = _load_launcher("start_discord_bot.py")
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(module, "BOT_LOG", tmp_path / "bot.log")
    monkeypatch.setattr(sys, "argv", ["start_discord_bot.py"])
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []
    monkeypatch.setattr(module, "stream_child",
                        _clocked_child(clock, rounds, spawns))
    with pytest.raises(_NoMoreRounds):
        module.main()
    return clock.slept, len(spawns)


def test_the_bot_launcher_still_judges_a_run_by_its_real_length(monkeypatch,
                                                                tmp_path):
    """基準（沒有時鐘跳動）：第二輪活很久 → 退避打回 5 秒；活很短 → 翻倍成 10。"""
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (120.0, 0.0, 1)])
    assert slept == [5, 5], f"長跑之後退避沒有被打回最小值：{slept}"

    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (2.0, 0.0, 1)])
    assert slept == [5, 10], f"連續兩次快速崩潰，退避沒有翻倍：{slept}"


def test_the_bot_launchers_backoff_ignores_a_backwards_clock_step(monkeypatch,
                                                                  tmp_path):
    """牆鐘往回撥不得把一次健康的執行變成「又崩潰了」。

    第二輪真的活了 120 秒，但牆鐘被往回撥 10 分鐘。用 `time.time()` 量會得到
    −480 秒 → 判成不健康 → 退避繼續往上長（10 秒），而它其實應該被打回 5 秒。
    """
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (120.0, -600.0, 1)])
    assert slept == [5, 5], (
        f"退避序列被牆鐘的往回跳影響了：{slept}（預期 [5, 5]）。"
        "一次活了 120 秒的執行是健康的，跟牆鐘怎麼跳沒有關係。")


def test_the_bot_launchers_backoff_ignores_a_forwards_clock_step(monkeypatch,
                                                                 tmp_path):
    """牆鐘往前撥不得把一次快速崩潰洗成健康執行。

    **危險的那個方向**：兩輪都是 2 秒就崩潰，但第二輪期間牆鐘往前跳 10 分鐘。用
    `time.time()` 量會判成健康 → 退避被打回 5 秒，於是一個 token 壞掉的 bot 會以
    近乎固定的 5 秒間隔一直重連——正是這段指數退避要避免的事。
    """
    slept, _spawns = _bot_launcher_with_clock(
        monkeypatch, tmp_path, [(2.0, 0.0, 1), (2.0, 600.0, 1)])
    assert slept == [5, 10], (
        f"退避序列被牆鐘的往前跳影響了：{slept}（預期 [5, 10]）。"
        "牆鐘往前跳會把快速崩潰洗成健康執行，退避因此被重置。")


# repo root 的**所有**腳本，不只兩支啟動器。這個缺陷會活下來的根本原因就是
# 「沒有任何掃描器涵蓋這個目錄」：2026-09-06 那次全專案時鐘掃描的兩支 AST 守門
# （`test_bot_helpers`、`test_webrunner_shared`）掃的都是 `axiomatic/`，而啟動器
# 住在上一層。列表在 import 時從真的 repo root 算出來，所以之後新增的 repo root
# 腳本會**自動**被納入，不必有人記得回來加名字。
_ROOT_SCRIPTS = sorted(
    name for name in os.listdir(REPO_ROOT)
    if name.endswith(".py")
    and os.path.isfile(os.path.join(REPO_ROOT, name))
)


@pytest.mark.parametrize("launcher", _ROOT_SCRIPTS)
def test_no_launcher_measures_an_interval_with_the_wall_clock(launcher):
    """靜態補一刀：repo root 的腳本裡都不得出現 `time.time() - x` 這個形狀。

    上面那六支行為測試釘的是**現在這兩個**判定；這一支擋的是**下一個**被加進來的
    牆鐘間隔——行為測試看不到還沒被寫出來的程式碼。這裡用 AST 是合適的，因為要
    驗的本來就是「原始碼長什麼樣」這種性質（不像「這個判斷擋不擋得住事情」，那種
    可以被恆假條件繞過，只能用行為釘）。

    寫時間戳（`time.time()` 單獨出現）不受影響——只有**相減**才是在量間隔。

    範圍是 repo root 的**每一支**腳本而不只兩支啟動器，因為這個缺陷能活下來的
    根本原因就是這個目錄沒有任何掃描器涵蓋（2026-09-06 那次掃描的兩支守門都只掃
    `axiomatic/`）。只修好兩支、守門也只看那兩支的話，下一支放在 repo root 的
    腳本會重蹈覆轍。
    """
    path = os.path.join(REPO_ROOT, launcher)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    def _is_wall_clock(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time")

    offenders = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
        and (_is_wall_clock(node.left) or _is_wall_clock(node.right))
    ]
    assert not offenders, (
        f"{launcher} 用牆鐘量間隔：{offenders}。行程內的間隔要用 "
        "`time.monotonic()`——`time.time()` 會被 NTP step 修正／改時鐘／快照還原"
        "跳動，往前跳會讓監督者無限重生一個壞掉的子行程。")


# ---------------------------------------------------------------------------
# DoD #5 的**行為**面：真的去呼叫 `python_command()`（2026-09-08）
#
# 上面那兩支守門（`test_the_interpreter_discovery_order_is_intact` /
# `test_both_launchers_discover_the_interpreter_the_same_way`）掃的是 AST——它們
# 看得到「候選的排列順序」，看不到「那個順序有沒有真的生效」。在這一節之前，
# **沒有任何一支測試呼叫過 `python_command()`**，而 `CLAUDE.md` 的 DoD #5 把它列
# 成硬規則：fresh clone 的「裝好就能跑」整個押在這個函式上。
#
# AST 抓不到、只有實跑才抓得到的四種寫壞法：
#
#   1. `venv_py.exists()` 被寫反（或那個路徑根本指錯地方）——`return [str(venv_py)]`
#      這一行原封不動，AST 完全看不出差別。
#   2. Windows／POSIX 挑錯子目錄（`Scripts` vs `bin`）。**這一半在這台機器上永遠
#      不會執行到**，所以更需要被測；`os.name` 換成假的就跑得到（同一條教訓見
#      上面的 POSIX flock 那一節）。
#   3. `shutil.which("py")` 的結果沒被用進回傳值。
#   4. `["py", "-3"]` 的 `-3` 掉了——fresh clone 會撞上系統預設的那個直譯器。
#
# **一律 monkeypatch `REPO_ROOT` 到暫存目錄**：這台機器上的 `.venv` 是正式行程正在
# 用的那一份，測試連讀都不該讀到它，更不可能去建立或刪除。
# ---------------------------------------------------------------------------


class _FakeOsName:
    """只提供 `name` 的假 `os`。

    刻意**不**寫 `setattr(os, "name", "posix")`：那會改到整個行程看到的 `os.name`，
    而本檔還有 daemon 抽水執行緒在跑。換掉的是**啟動器模組自己的** `os` 名字繫結，
    而且每一支測試都用 `_load_launcher` 重新載一份模組，所以影響範圍就是這一支。
    """

    def __init__(self, name: str):
        self.name = name


class _FakeShutil:
    """假的 `shutil`：`which` 回固定值，並記下被問過什麼。

    `asked` 是短路的證據——`.venv` 命中時這裡必須是空的，否則就代表 `.venv` 那一步
    沒有真的排在前面（AST 守門看不出這件事）。
    """

    def __init__(self, which_result):
        self._which = which_result
        self.asked: list[str] = []

    def which(self, cmd):
        self.asked.append(cmd)
        return self._which


_VENV_LAYOUT = {
    "nt": (".venv", "Scripts", "python.exe"),
    "posix": (".venv", "bin", "python"),
}
_FAKE_PY_LAUNCHER = os.path.join("C:\\", "Windows", "py.exe")


@contextlib.contextmanager
def _interpreter_probe(launcher, root, *, os_name, venv=None, which=None):
    """載入啟動器，把 `REPO_ROOT` / `os` / `shutil` 換成假的，yield 出模組。

    `root` 一定是暫存目錄——真的 repo `.venv` 一根寒毛都不會被碰到。
    `venv` 是要在 `root` 底下先建出來的假直譯器（相對路徑 tuple），`None` ＝ 這個
    clone 沒有 `.venv`。`which` 是 `shutil.which("py")` 要回的東西。

    用 `_Swapped` 而不是 `monkeypatch` fixture，理由與 POSIX flock 那一節相同：
    本檔的 standalone runner 沒有 fixture，用 fixture 的話這幾支會整組消失。
    """
    module = _load_launcher(launcher)
    root = pathlib.Path(root)
    venv_py = None
    if venv is not None:
        venv_py = root.joinpath(*venv)
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("# 假的直譯器，只會被 exists() 看到，不會被執行\n",
                           encoding="utf-8")
    fake_shutil = _FakeShutil(which)
    with contextlib.ExitStack() as stack:
        stack.enter_context(_Swapped(module, "REPO_ROOT", root))
        stack.enter_context(_Swapped(module, "os", _FakeOsName(os_name)))
        stack.enter_context(_Swapped(module, "shutil", fake_shutil))
        yield module, venv_py, fake_shutil


@pytest.mark.parametrize("launcher", _LAUNCHERS)
@pytest.mark.parametrize("os_name", sorted(_VENV_LAYOUT))
def test_a_clone_with_a_venv_runs_that_exact_interpreter(launcher, os_name,
                                                         tmp_path):
    """`.venv` 在的時候要回**那個絕對路徑**，而且兩個平台的子目錄都要挑對。

    POSIX 那一半在這台 Windows 機器上永遠不會自然執行到，所以只有把 `os.name`
    換掉才驗得到。挑錯子目錄的後果不是報錯而是**靜默降級**：`.venv/bin/python`
    在 Windows 上不存在 → 落到 `py -3` → 正式行程換成系統直譯器在跑，而相依套件
    的版本當場分岔（那正是「這台機器上有三組相依」那個陷阱的來源）。
    """
    with _interpreter_probe(launcher, tmp_path, os_name=os_name,
                            venv=_VENV_LAYOUT[os_name],
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, sh):
        got = mod.python_command()
    assert got == [str(venv_py)], (
        f"{launcher} 在 os.name={os_name!r}、`.venv` 存在的情況下回了 {got}，"
        f"應該是 [{str(venv_py)!r}]。DoD #5：本機 `.venv` 排第一。")
    assert os.path.isabs(got[0]), (
        f"{launcher} 回的不是絕對路徑（{got[0]!r}）——子行程的工作目錄是 repo "
        "root，相對路徑只是剛好會動，換個地方就找不到。")
    assert sh.asked == [], (
        f"{launcher} 明明已經找到 `.venv` 了，卻還去問 `shutil.which({sh.asked})`"
        "——代表 `.venv` 那一步沒有真的排在 `py -3` 前面（AST 守門看不出這件事，"
        "因為兩個 return 的字面內容都沒變）。")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
@pytest.mark.parametrize("os_name", sorted(_VENV_LAYOUT))
def test_the_other_platforms_venv_layout_is_not_accepted(launcher, os_name,
                                                         tmp_path):
    """只有**另一個**平台的 `.venv` 佈局存在時，不得把它當成命中。

    這是上一支的反向。少了它，把兩個分支的子目錄對調照樣全綠——因為每一支測試都
    只餵它自己那個佈局。
    """
    other = "posix" if os_name == "nt" else "nt"
    with _interpreter_probe(launcher, tmp_path, os_name=os_name,
                            venv=_VENV_LAYOUT[other],
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, _sh):
        got = mod.python_command()
    assert got == [_FAKE_PY_LAUNCHER, "-3"], (
        f"{launcher} 在 os.name={os_name!r} 底下把 {other} 的佈局"
        f"（{venv_py}）當成了可用的直譯器，回了 {got}。兩個分支的子目錄挑反了。")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_fresh_clone_without_a_venv_falls_back_to_the_py_launcher(launcher,
                                                                    tmp_path):
    """沒有 `.venv` 的 fresh clone 要落到 `py -3`，而且 `-3` 不可以掉。

    `-3` 是這一步唯一真正致命的部分：少了它，`py` 會挑系統預設的那一版——可能是
    Python 2，也可能是別的 3.x——於是 `py -3 -m venv .venv` 都還沒跑的人第一次啟動
    就會撞上一個看不懂的 import 失敗。AST 那支只確認「有一個 return 裡出現 `-3`」，
    確認不了它真的被回傳出去。
    """
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=None,
                            which=_FAKE_PY_LAUNCHER) as (mod, _venv, sh):
        got = mod.python_command()
    assert got == [_FAKE_PY_LAUNCHER, "-3"], (
        f"{launcher} 沒有 `.venv` 時回了 {got}，應該是 "
        f"[{_FAKE_PY_LAUNCHER!r}, '-3']。")
    assert sh.asked == ["py"], (
        f"{launcher} 問的不是 `py`（實際問了 {sh.asked}）——DoD #5 指名的是 Windows "
        "的 py 啟動器，它才會避開 Microsoft Store 那個殼。")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_without_a_venv_or_a_py_launcher_it_uses_the_current_interpreter(
        launcher, tmp_path):
    """兩個都沒有時，用啟動這支啟動器的那個直譯器——最後一道保底，不能回空的。"""
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=None,
                            which=None) as (mod, _venv, _sh):
        got = mod.python_command()
    assert got == [sys.executable], (
        f"{launcher} 在既沒有 `.venv` 也沒有 `py` 的機器上回了 {got}，"
        f"應該是 [{sys.executable!r}]。這是保底那一步，回錯就等於根本啟動不了。")


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_the_venv_beats_the_py_launcher_when_both_are_available(launcher,
                                                                tmp_path):
    """**順序真的是順序**：兩個都在的時候必須選 `.venv`。

    這台開發機正是「兩個都在」，所以這支測的就是正式行程每天實際走的那條路。
    選錯的話 bot 與 webrunner 會跑在系統直譯器上——症狀不是啟動失敗，而是相依
    套件的版本悄悄換了一組。
    """
    layout = _VENV_LAYOUT["nt" if os.name == "nt" else "posix"]
    with _interpreter_probe(launcher, tmp_path, os_name=os.name, venv=layout,
                            which=_FAKE_PY_LAUNCHER) as (mod, venv_py, sh):
        got = mod.python_command()
    assert got == [str(venv_py)], (
        f"{launcher} 在 `.venv` 與 `py` 都存在時選了 {got}，應該選 `.venv`"
        f"（{venv_py}）。")
    assert sh.asked == [], "選到 `.venv` 之後不該再去問 `py`"


# ---------------------------------------------------------------------------
# `python_command()` 一共有**三份**，第三份刻意不一樣
# ---------------------------------------------------------------------------
# 兩支啟動器那兩份必須逐字相同（上面那支 AST 守門在盯）。`install_autostart.py`
# 的第三份**刻意不同**，理由寫在它自己的 docstring 裡：工作排程器跑的時候 PATH 與
# 環境變數跟互動 shell 不同，所以不走 `py -3` 而是寫死絕對路徑；也刻意不用
# `pythonw.exe`（排程器啟動的 `pythonw` 沒有繼承標準控制代碼，`sys.stdout` 是
# `None`，而這整套到處都在 `print()`，第一行就會炸成 `AttributeError`）。
#
# 少了下面這兩支，讀到「兩支啟動器要逐字相同」的人很容易把第三份當成**漏掉的**
# 那一份而順手「統一」掉——那個改動看起來完全無害，代價是開機自動啟動的那一套
# 再也起不來，而且沒有任何錯誤訊息。
# **兩份名單要對得起來（2026-09-11 修）。** `_LAUNCHERS`（本檔上方）驅動十幾支
# parametrize 過的守門——探索順序、逐字相同、單一實例鎖、降級措辭、單調時鐘、
# `stream_child`……，而下面這份表只回答「這份複本有沒有人認領」。在此之前兩者**互不
# 相干**：新增一支 `start_thing.py` 會讓下面那支變紅，而最便宜的修法是補一行字串；
# 補完全綠，那支新啟動器的探索順序卻**一支守門都沒有**。紅燈還反過來教人「登記就算
# 處理完了」，那比沒有那盞燈更糟。所以「啟動器」那一類現在**由 `_LAUNCHERS` 算出
# 來**，不另抄一份——歸類即涵蓋。
#
# 例外名單：**只給「刻意做相反的事」的那幾份**，不是給新啟動器的逃生門。一筆過期的
# 例外是 fail-open——檔案改名、或那個 `python_command()` 被拿掉之後，那個字串就再也
# 對不到任何東西，守門照跑、測試全綠，而受檢集合已經悄悄少了一個（`CLAUDE.md` 對
# `_OWNER_ONLY_SLASH` 記的是同一個形狀）。
_PYTHON_COMMAND_EXEMPT = {
    "install_autostart.py":
        "工作排程器專用，**刻意分歧**。不走 `py -3`：`py.exe` 是轉接器，`-3` 要到"
        "執行當下才去查登錄檔、`PY_PYTHON`、`py.ini` 與 shebang，登入工作拿到的環境"
        "跟互動 shell 不一樣，而且它一定不會解析到 `.venv`。也只找 "
        "`.venv/Scripts/`，因為 `main()` 在非 Windows 上就直接收工。"
        "理由全文見該函式的 docstring。",
}


def _root_python_command_files() -> set:
    """repo root 上**模組層**定義了 `python_command()` 的檔案，由 AST 算出來。

    只看 `tree.body` 而不是 `ast.walk`：巢狀在函式或類別裡的同名定義不是 DoD #5
    那個交接點，收進來只會製造誤報。
    """
    found = set()
    for name in _ROOT_SCRIPTS:
        tree = ast.parse(
            pathlib.Path(REPO_ROOT, name).read_text(encoding="utf-8"))
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "python_command"
               for node in tree.body):
            found.add(name)
    return found


def _python_command_registration_errors(found, launchers, exempt, *,
                                        min_found=3, min_launchers=2) -> list:
    """歸類表的對帳判準。**純函式**——理由同 `test_verify_browser._exemption_errors`：
    現況是乾淨的，所以把主測試裡的斷言整條刪掉本來就不會紅，牙齒得長在一個合成語料
    問得到的地方。
    """
    errors = []
    # (A) 族群下限。空的 `found` 會讓 (C)(D)(E) 三條**真空成立**，輸出跟「全部合規」
    #     一模一樣；`_ROOT_SCRIPTS` 只要掃錯目錄就是這個下場。
    if len(found) < min_found:
        errors.append(
            f"repo root 只掃到 {len(found)} 份 `python_command()`（下限 "
            f"{min_found}）：{sorted(found)}。空的受檢集合會讓下面每一條對帳真空"
            "通過，而輸出跟「全部合規」分不出來。")
    # (B) `_LAUNCHERS` 的下限。它是十幾支 parametrize 守門的參數來源，而 pytest 對
    #     **空的**參數集是 skipped、不是 error——清空它等於靜默關掉那十幾支。
    if len(launchers) < min_launchers:
        errors.append(
            f"`_LAUNCHERS` 只剩 {len(launchers)} 支（下限 {min_launchers}）："
            f"{sorted(launchers)}。空的參數集在 pytest 只是少幾行輸出，不會紅。")
    # (C) 每一份複本都要被歸類。新增一支啟動器時這條先紅。
    unclaimed = set(found) - set(launchers) - set(exempt)
    if unclaimed:
        errors.append(
            f"這幾份 `python_command()` 沒有被歸類：{sorted(unclaimed)}。它要嘛是"
            "啟動器（加進 `_LAUNCHERS`，那十幾支守門就會自動涵蓋它，探索順序與逐字"
            "相同都會被檢查），要嘛是刻意分歧（加進 `_PYTHON_COMMAND_EXEMPT` 並寫下"
            "理由）。**只補一行登記是不夠的**——那正是這條在擋的事。")
    # (D) 反方向：`_LAUNCHERS` 裡的名字必須還真的有 `python_command()`。
    stale = set(launchers) - set(found)
    if stale:
        errors.append(
            f"`_LAUNCHERS` 裡的 {sorted(stale)} 已經不是 `python_command()` 的"
            f"所在地了（檔案改名、或那個函式被拿掉）。目前掃到的是：{sorted(found)}")
    # (E) 反方向：例外名單的 fail-open 那一面，外加理由不得敷衍。
    for name, reason in exempt.items():
        if name not in found:
            errors.append(
                f"例外名單裡的 {name!r} 已經沒有 `python_command()` 了（檔案改名、"
                f"或那個函式被拿掉）。目前掃到的是：{sorted(found)}")
        if len(str(reason).strip()) < 20:
            errors.append(f"{name} 的例外沒有寫下夠具體的理由：{reason!r}")
    # (F) 同一個檔案不得兩邊都列——那是兩種相反的處置，重疊時「該不該逐字相同」沒有
    #     答案，而測試會照 `_LAUNCHERS` 那邊跑，等於例外被默默忽略。
    both = set(launchers) & set(exempt)
    if both:
        errors.append(
            f"{sorted(both)} 同時列在 `_LAUNCHERS` 與 `_PYTHON_COMMAND_EXEMPT`。"
            "那是兩種相反的處置（必須逐字相同／刻意分歧），挑一邊。")
    return errors


def test_every_copy_of_python_command_is_accounted_for():
    """repo root 的每一份 `python_command()` 都要被**歸類**，不只是被登記。

    fail-closed：第四份出現時這支會紅，而訊息會逼寫的人回答「它該跟誰一致」。沒有
    這一層的話，一份沒人知道的複本可以帶著自己那套探索順序活很久。

    **2026-09-11 加強。** 以前它問的是「登記表有沒有這個名字」，而那可以用一行字串
    滿足——字串是散文，沒有任何東西能從中判斷那個檔案該不該進 `_LAUNCHERS`。於是
    「補一行登記」看起來就是完整的修法，新啟動器的探索順序一支守門都不會看，而紅燈
    本身還背書了那個錯誤的修法。現在啟動器那一類由 `_LAUNCHERS` 算出來，**歸類即
    涵蓋**。
    """
    problems = _python_command_registration_errors(
        _root_python_command_files(), _LAUNCHERS, _PYTHON_COMMAND_EXEMPT)
    assert not problems, "\n".join(problems)


def test_the_python_command_registration_bites_on_a_synthetic_corpus():
    """對照組：真實資料是乾淨的，所以上面那支**刪掉任何一條分支都不會紅**。

    每一條各給一份**只違反它自己**的語料。用同一份語料的話兩條會互相遮蔽——刪掉其中
    一條，另一條照樣把那份語料判成有問題，變異就活下來了（這一輪在
    `_collect_codex_images` 上剛踩過同一個形狀）。
    """
    clean_found = {"a.py", "b.py", "x.py"}
    clean_launchers = ("a.py", "b.py")
    clean_exempt = {"x.py": "刻意分歧，這段理由寫得夠長，足以通過長度下限。"}

    def _errors(found=None, launchers=None, exempt=None):
        return _python_command_registration_errors(
            clean_found if found is None else found,
            clean_launchers if launchers is None else launchers,
            clean_exempt if exempt is None else exempt)

    assert _errors() == [], (
        f"對照語料本身就該是乾淨的，否則下面每一條都證明不了是自己咬的：{_errors()}")

    cases = [
        ("A 族群下限", dict(found={"a.py", "b.py"}, exempt={}), "下限 3"),
        ("B `_LAUNCHERS` 下限",
         dict(found={"a.py", "x.py", "y.py"}, launchers=("a.py",),
              exempt={"x.py": clean_exempt["x.py"],
                      "y.py": clean_exempt["x.py"]}), "下限 2"),
        ("C 新複本沒有被歸類",
         dict(found=clean_found | {"start_thing.py"}), "start_thing.py"),
        ("D `_LAUNCHERS` 裡有過期的名字",
         dict(launchers=clean_launchers + ("gone.py",)), "gone.py"),
        ("E1 例外名單裡有過期的名字",
         dict(exempt={**clean_exempt, "gone.py": clean_exempt["x.py"]}),
         "gone.py"),
        ("E2 例外的理由太敷衍", dict(exempt={"x.py": "太短"}), "夠具體的理由"),
        ("F 同一個檔案兩邊都列",
         dict(exempt={"a.py": clean_exempt["x.py"],
                      "x.py": clean_exempt["x.py"]}), "挑一邊"),
    ]
    for label, corpus, needle in cases:
        got = _errors(**corpus)
        assert len(got) == 1, (
            f"{label}：這份語料應該**只**觸發一條，實際 {len(got)} 條。兩條以上代表"
            f"語料沒隔乾淨，刪掉其中一條分支仍然會被另一條遮住。\n{got}")
        assert needle in got[0], f"{label}：訊息沒提到 {needle!r}：{got[0]}"


def test_the_autostart_copy_is_deliberately_different_not_a_missed_one():
    """第三份**不得**被「統一」成啟動器那一份。

    斷言的是不相等，看起來反直覺，但這正是要保護的性質：把 `py -3` 塞回排程器那
    條路，工作排程器的環境裡 PATH 不一樣，找到的可能是另一個直譯器甚至找不到；而
    這種失敗只會在下次重新開機時出現，現場沒有人在看。
    """
    autostart = ast.unparse(_python_command_node("install_autostart.py"))
    launcher = ast.unparse(_python_command_node("start_discord_bot.py"))
    assert autostart != launcher, (
        "`install_autostart.python_command()` 被改成跟啟動器那份一樣了。那份是"
        "**刻意分歧**，不是漏掉的複本：排程器的 PATH 與互動 shell 不同，所以它"
        "寫死 `.venv/Scripts/` 的絕對路徑而不走 `py -3`。要改的話先讀它的 "
        "docstring，再更新 `_PYTHON_COMMAND_EXEMPT` 的說明。")
    assert "'-3'" not in autostart and '"-3"' not in autostart, (
        f"`install_autostart.python_command()` 長出了 `py -3`：{autostart}")


# ---------------------------------------------------------------------------
# 兩支啟動器的收尾與失敗路徑（2026-09-08 補）
#
# 量覆蓋率時發現：`_supervisor.py` 是 100%，但兩支啟動器不是——漏掉的整片都是
# 「已經出事之後」才會執行的路徑。與 `_supervisor.py` 那一節同樣的判準：監督者
# 存在的唯一理由就是別的東西壞掉的時候撐住，所以那些分支等於它最核心的職責。
#
# 這一節的每一支都**不會真的起出 bot 或 webrunner**：`stream_child` 一律換成
# 間諜或會丟例外的攔截器。webrunner 一啟動就無條件 nuclear sweep 掉全機 Chrome，
# 而這台機器上通常有一個跑了好幾天的無人值守批次在用它。
#
# **假子行程一律有上限**：超過預期輪數就丟 `_NoMoreRounds` 把迴圈拆掉。兩支監督者
# 的迴圈都是 `while True`，而假時鐘的 `sleep` 不會真的等，所以「這條早退路徑被改壞
# 了」的症狀會是**測試掛住**而不是紅掉——變異測試實際踩到：把 `child_exit_is_fatal`
# 那個分支改成恆假之後，測試不是紅，是一路轉到 `attempt 7088727` 才被逾時砍掉。
# 掛住的測試比紅掉的更糟：紅的會指出問題，掛住的只是讓整輪停在那裡。
# ---------------------------------------------------------------------------

def _expect_no_extra_round(call, what):
    """跑 `call()`，如果監督者又轉了一輪就以看得懂的訊息失敗。

    假子行程的上限是用 `_NoMoreRounds` 做的（見本節開頭），而一個裸的
    `_NoMoreRounds` 只說明「被呼叫太多次」，沒說為什麼那是錯的。把它翻譯回原本
    要保護的性質，下一個看到紅燈的人才不用回頭讀測試的實作。
    """
    try:
        return call()
    except _NoMoreRounds:
        pytest.fail(
            f"{what}——監督者卻又重生了一輪。這條早退路徑必須當場結束迴圈："
            "照常退避的話它會一直重生一個註定用同樣方式失敗的子行程，而 "
            "rapid-fail giveup 只看「活了多久」，看不出這件事。")


BOT_LAUNCHER_NAME = "start_discord_bot.py"


class _SpyLock:
    """假的單一實例鎖：記下被放掉幾次。

    換掉真的那把是**安全需求**：真的會去動 repo root 的
    `.discord_bot_supervisor.lock`／`.webrunner_supervisor.lock`，而正式的監督者
    正握著它們。
    """

    def __init__(self, *, degraded=False):
        self.degraded = degraded
        self.releases = 0

    def release(self):
        self.releases += 1


class _CtrlCOnSleep(_Clock):
    """退避睡到一半被 Ctrl+C。"""

    def sleep(self, seconds):
        self.slept.append(seconds)
        raise KeyboardInterrupt


def _bot_launcher(monkeypatch, tmp_path, child, *, lock=None, clock=None,
                  log_path=None):
    """把 `start_discord_bot` 架在替身上，回 `(module, lock, clock)`。

    `child` 就是假的 `stream_child`。呼叫端自己決定什麼時候呼叫 `module.main()`。
    """
    module = _load_launcher(BOT_LAUNCHER_NAME)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(module, "BOT_LOG",
                        log_path if log_path is not None
                        else tmp_path / "bot.log")
    monkeypatch.setattr(sys, "argv", [BOT_LAUNCHER_NAME])
    lock = _SpyLock() if lock is None else lock
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: lock)
    clock = _Clock() if clock is None else clock
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "stream_child", child)
    return module, lock, clock


def test_the_bot_launcher_stops_instead_of_respawning_a_doomed_child(
        monkeypatch, tmp_path):
    """bot 回報「已經有另一個實例在跑」時，啟動器要收工，不是退避重生。

    這條分支的註解自己說明了理由：bot 本體有它自己的一把鎖，被擋掉時每次都在一秒
    內乾淨退出。照常退避的話這個迴圈會每 5～300 秒重生一次**註定被同一把鎖擋掉**
    的子行程，而 rapid-fail giveup 只看「活了多久」，看不出這件事——它會永遠轉下去。
    """
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        return RC_ALREADY_RUNNING

    log_path = tmp_path / "bot.log"
    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                        log_path=log_path)
    rc = _expect_no_extra_round(module.main,
                                "bot 回報已經有另一個實例在執行")

    assert rc == RC_ALREADY_RUNNING, (
        f"致命 rc 應該原樣往外送（好讓外面的排程器也看得出來），實際回 {rc}")
    assert len(spawns) == 1, (
        f"重生了 {len(spawns)} 次。這種失敗重試永遠不會成功，迴圈必須當場停下來。")
    assert clock.slept == [], (
        f"還睡了退避 {clock.slept}——代表走的是一般的崩潰路徑，不是這條。")
    assert lock.releases == 1, f"鎖沒有在收工時放掉（releases={lock.releases}）"
    assert "另一個實例" in log_path.read_text(encoding="utf-8"), (
        "收工了卻沒有把原因寫進記錄檔。這條路上使用者看到的就是啟動器直接結束，"
        "沒有那行說明的話沒人知道該去結束哪一個行程。（改了措辭就更新這支測試。）")


def test_the_bot_launcher_holds_the_lock_until_the_loop_is_over(monkeypatch,
                                                                tmp_path):
    """鎖要**整段**握著，而且離開時一定放得掉（`finally` 裡那一次）。

    兩個方向一起釘：迴圈跑到一半就放掉的話，第二個監督者當場起得來；而 `release()`
    如果不在 `finally` 裡，任何往外炸的例外都會讓它被跳過。
    """
    held_during_run = []

    def _child(cmd, _log, **_kwargs):
        held_during_run.append(lock.releases)
        raise _NoMoreRounds

    lock = _SpyLock()
    module, lock, _clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                         lock=lock)
    with pytest.raises(_NoMoreRounds):
        module.main()

    assert held_during_run == [0], (
        "子行程還在跑的時候鎖就被放掉了——這段期間第二個監督者起得來，"
        "而兩套都會「看起來正常」。")
    assert lock.releases == 1, (
        f"往外炸的例外把 `lock.release()` 跳過了（releases={lock.releases}）。"
        "它必須在 `finally` 裡。")


def test_a_ctrl_c_while_the_bot_runs_exits_cleanly_without_respawning(
        monkeypatch, tmp_path):
    """Ctrl+C 是**乾淨結束**（rc=0），不是一次崩潰——不得再重生一輪。"""
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        raise KeyboardInterrupt

    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child)
    rc = _expect_no_extra_round(module.main, "子行程執行中收到 Ctrl+C")

    assert rc == 0, f"Ctrl+C 應該回 rc=0，實際 {rc}"
    assert len(spawns) == 1, "Ctrl+C 之後又重生了一輪"
    assert clock.slept == [], "Ctrl+C 之後還去睡退避"
    assert lock.releases == 1, "Ctrl+C 之後鎖沒放掉"


def test_a_ctrl_c_during_the_backoff_sleep_also_exits_cleanly(monkeypatch,
                                                              tmp_path):
    """等退避的那幾分鐘正是最可能被按 Ctrl+C 的時候——那條路要單獨有出口。

    這是兩個 Ctrl+C 出口裡比較容易被漏掉的一個：迴圈大部分時間其實停在這裡。
    """
    spawns = []

    def _child(cmd, _log, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        return 1

    module, lock, clock = _bot_launcher(monkeypatch, tmp_path, _child,
                                        clock=_CtrlCOnSleep())
    rc = _expect_no_extra_round(module.main, "等退避的時候收到 Ctrl+C")

    assert rc == 0, f"睡到一半的 Ctrl+C 應該回 rc=0，實際 {rc}"
    assert clock.slept == [5], (
        f"第一次崩潰應該睡最小退避 5 秒（實際 {clock.slept}）")
    assert len(spawns) == 1, "Ctrl+C 之後又重生了一輪"
    assert lock.releases == 1


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_a_log_file_that_will_not_open_does_not_stop_the_launcher(launcher,
                                                                  tmp_path,
                                                                  monkeypatch):
    """記錄檔開不起來只能降級成「只有主控台」，**不能**因此不啟動。

    判準寫在兩支啟動器的註解裡：監督者不該因為一個附屬功能而拒絕啟動 bot／整批
    產圖。用一個**目錄**當記錄檔路徑來製造真的 `OSError`（實測 Windows 給
    `PermissionError`），比替換 `open` 更貼近真實失敗。
    """
    as_a_log = tmp_path / "log_is_a_directory"
    as_a_log.mkdir()

    spawns = []

    def _child(_cmd, log, **_kwargs):
        spawns.append(log)
        raise _NoMoreRounds

    module = _load_launcher(launcher)
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: _SpyLock())
    monkeypatch.setattr(sys, "argv", [launcher])
    monkeypatch.setattr(
        module,
        "WEBRUNNER_LOG" if "webrunner" in launcher else "BOT_LOG",
        as_a_log)
    monkeypatch.setattr(module, "stream_child", _child)
    if hasattr(module, "_chrome_slot"):
        monkeypatch.setattr(module, "_chrome_slot", _FakeSlot())
        monkeypatch.setattr(module, "WEBRUNNER_PID_FILE",
                            tmp_path / "webrunner.pid")

    with pytest.raises(_NoMoreRounds):
        module.main()

    assert spawns == [None], (
        f"{launcher} 在記錄檔開不起來時沒有降級成「只有主控台」"
        f"（傳給 stream_child 的 log 是 {spawns}）——它應該照常啟動子行程，"
        "只是不寫檔。")


# ---------------------------------------------------------------------------
# start_webrunner：Chrome 槽的短臨界區與早退路徑
# ---------------------------------------------------------------------------


class _OrderingSlot(_FakeSlot):
    """`_FakeSlot` 加一件事：每次 `release` 時記下 pid 檔當下的內容。

    這是「**先寫 pid 再放槽**」那條順序契約唯一測得出來的方式——兩個動作都做了、
    只是順序反過來的話，事後看不出任何差別，但中間那一瞬間槽是空的而 pid 還沒寫，
    驗證端剛好在那時取槽就會判定沒人在跑，開出第二個 Chrome stack。
    """

    def __init__(self, pid_file, *, acquired=True, pid_alive=True):
        super().__init__(pid_alive=pid_alive)
        self._pid_file = pathlib.Path(pid_file)
        self._acquired = acquired
        self.pid_at_release: list[str | None] = []

    def acquire(self, owner, *, timeout=0.0, label=""):
        return self._acquired

    def release(self, owner):
        super().release(owner)
        self.pid_at_release.append(
            self._pid_file.read_text(encoding="utf-8")
            if self._pid_file.exists() else None)


class _SpawnedProc:
    """`stream_child` 交給 `on_spawn` 的那個 `Popen` 只被讀 `.pid`。

    名字刻意不叫 `_FakeProc`——本檔上面收屍那一節已經有一個同名的類別，蓋掉它會
    讓那五支測試以 `AttributeError` 紅掉（實際踩過）。
    """

    def __init__(self, pid: int):
        self.pid = pid


def _webrunner_module(monkeypatch, tmp_path, *, slot=None):
    """載入 `start_webrunner`，把 pid 檔與 Chrome 槽都指到暫存目錄。

    **正式的 `webrunner.pid` 與 `chrome_slot.lock` 一律不碰**：這台機器上常有一個
    跑了幾十小時的批次正在用它們。
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    pid_file = tmp_path / "webrunner.pid"
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", pid_file)
    slot = _OrderingSlot(pid_file) if slot is None else slot
    monkeypatch.setattr(module, "_chrome_slot", slot)
    return module, slot, pid_file


def _supervise_once(module, log=None, **overrides):
    params = dict(backoff_min=5.0, backoff_max=300.0, healthy_sec=60.0,
                  rapid_threshold_sec=30.0, rapid_giveup=3,
                  zero_progress_giveup=3)
    params.update(overrides)
    return module._supervise(["python", "-u", "webrunner.py"], "selenium", log,
                             **params)


def test_the_pid_is_on_disk_before_the_chrome_slot_is_released(monkeypatch,
                                                               tmp_path):
    """短臨界區的順序契約：**取槽 → spawn → 寫 pid → 放槽**。

    反過來寫（先放槽再寫 pid）會留下一個空窗：槽是空的、pid 還沒寫，驗證端在那一
    瞬間取槽就會判定「沒有批次在跑」，於是對著同一份登入 profile 開出第二個 Chrome
    stack，接著被 webrunner 的 per-character 重啟掃掉。事後兩邊的紀錄都看起來正常。
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    rounds = []

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        rounds.append(1)
        if len(rounds) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        on_spawn(_SpawnedProc(4242))
        return 0

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0

    assert slot.pid_at_release, "根本沒有放槽——槽會被佔到 staleness 逾時"
    assert slot.pid_at_release[0] == "4242", (
        f"放槽的那一刻 pid 檔的內容是 {slot.pid_at_release[0]!r}，應該已經是 "
        "'4242'。順序反了：槽空了、pid 還沒寫，驗證端會在那個空窗裡開出第二個 "
        "Chrome stack。")


def test_the_liveness_signal_is_taken_back_when_the_child_is_gone(monkeypatch,
                                                                  tmp_path):
    """子行程結束後要把 `webrunner.pid` 收回去（`finally` 裡那一步）。

    漏掉的話，那個檔案會一直宣告「批次還在跑」，驗證端從此永遠讓位——而且症狀是
    「驗證腳本一直 SKIP」，沒人會往這裡找。
    """
    module, _slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    rounds = []

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        rounds.append(1)
        if len(rounds) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        on_spawn(_SpawnedProc(4242))
        assert pid_file.exists(), "子行程在跑的時候 pid 檔就該在了"
        return 0

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0
    assert not pid_file.exists(), (
        "子行程已經結束，`webrunner.pid` 卻還留著——驗證端會永遠以為有批次在跑。")


def test_a_chrome_slot_that_never_frees_up_refuses_to_spawn(monkeypatch,
                                                            tmp_path):
    """等不到槽就 rc=1 收工，**不是**重試迴圈。

    等到逾時（300s > 驗證腳本自己的 240s 預算）還拿不到，代表不是「剛好排在驗證
    中間」，而是有人卡住了——那需要人看一眼。重要的是這條路**什麼都沒動**：沒有
    sweep、沒有 spawn。
    """
    pid_file = tmp_path / "webrunner.pid"
    slot = _OrderingSlot(pid_file, acquired=False)
    module, _slot, _pid = _webrunner_module(monkeypatch, tmp_path, slot=slot)

    def _child(*_args, **_kwargs):
        raise AssertionError("槽都還沒拿到就 spawn 了 webrunner")

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 1
    assert slot.released == [], (
        f"槽根本沒拿到卻去放了它：{slot.released}。`release` 只在本行程仍持有時"
        "刪檔，但這裡連呼叫都不該有——放掉的可能是別人的槽。")
    assert not pid_file.exists(), "沒有 spawn 卻寫了存活訊號"


def test_a_blocked_generation_stops_the_launcher_instead_of_respawning(
        monkeypatch, tmp_path):
    """rc=4（站方擋住生成）要當場停下來，不得退避重生。

    重生只會看到同一個對話框，還會每一輪重跑一次登入 ＋ setup。這條與 rapid-fail
    無關：被擋住的那一輪可能跑很久，rapid-fail 永遠不會響。
    """
    module, _slot, _pid = _webrunner_module(monkeypatch, tmp_path)
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    spawns = []

    def _child(cmd, _log, *, on_spawn=None, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        on_spawn(_SpawnedProc(4242))
        clock.advance(120.0)
        return 4

    monkeypatch.setattr(module, "stream_child", _child)
    assert _expect_no_extra_round(
        lambda: _supervise_once(module), "站方擋住了生成（rc=4）") == 1
    assert len(spawns) == 1, (
        f"生成被擋住卻還是重生了（spawn {len(spawns)} 次）——每一輪都會重跑一次"
        "登入與 setup，然後看到同一個對話框。")
    assert clock.slept == [], "被擋住之後還去睡退避"


def test_a_ctrl_c_during_a_batch_releases_the_slot_and_the_pid(monkeypatch,
                                                               tmp_path):
    """Ctrl+C 回 rc=0，而且 `finally` 那兩件收尾都要做完。

    漏掉任何一件，下一次啟動都會被自己上一次留下的東西擋住：槽要等 600 秒的
    staleness backstop 才回收，pid 檔則會讓驗證端一直讓位。
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)

    def _child(_cmd, _log, *, on_spawn=None, **_kwargs):
        on_spawn(_SpawnedProc(4242))
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "stream_child", _child)
    assert _supervise_once(module) == 0, "Ctrl+C 應該回 rc=0"
    assert not pid_file.exists(), "Ctrl+C 之後沒有收回存活訊號"
    assert slot.released == [module.SLOT_OWNER] * 2, (
        f"槽的釋放次數不對：{slot.released}（`_on_spawn` 一次、`finally` 一次；"
        "`release` 只在本行程仍持有時刪檔，所以重複呼叫是安全的）")


def test_a_ctrl_c_during_the_webrunners_backoff_also_exits_cleanly(monkeypatch,
                                                                   tmp_path):
    """webrunner 這一側的第二個 Ctrl+C 出口。

    退避最長 300 秒，所以「按下 Ctrl+C 的那一刻迴圈停在哪裡」多半就是這裡，不是
    子行程執行中。少了這個出口，KeyboardInterrupt 會直接往外炸成一段 traceback，
    而 `finally` 已經跑完了——看起來像壞掉，其實只是沒有人接。
    """
    module, slot, pid_file = _webrunner_module(monkeypatch, tmp_path)
    clock = _CtrlCOnSleep()
    monkeypatch.setattr(module, "time", clock)
    spawns = []

    def _child(cmd, _log, *, on_spawn=None, **_kwargs):
        spawns.append(cmd)
        if len(spawns) > 1:
            raise _NoMoreRounds          # 見下面「假子行程一律有上限」
        on_spawn(_SpawnedProc(4242))
        clock.advance(2.0)
        return 1

    monkeypatch.setattr(module, "stream_child", _child)
    assert _expect_no_extra_round(
        lambda: _supervise_once(module),
        "等退避的時候收到 Ctrl+C") == 0, "睡到一半的 Ctrl+C 應該回 rc=0"
    assert clock.slept == [5.0], (
        f"第一次崩潰應該睡最小退避 5 秒（實際 {clock.slept}）")
    assert len(spawns) == 1, "Ctrl+C 之後又重生了一輪"
    assert not pid_file.exists(), "Ctrl+C 之後沒有收回存活訊號"


def test_the_slow_zero_progress_gate_stops_what_rapid_fail_cannot(monkeypatch,
                                                                  tmp_path):
    """連續「跑很久卻一張都沒產出」要放棄——rapid-fail 抓不到這種。

    兩半一起測，因為單看其中一半都會被錯的實作騙過去：

    * 前半：每一輪都活 40 秒（> `rapid_fail_threshold_sec`，所以 rapid-fail 每輪
      歸零、永遠不會響）並回 rc=3，兩輪之後要放棄。
    * 後半：同樣的輸入、只是把 `zero_progress_giveup` 調高，就必須繼續重生——
      否則「永遠放棄」也會讓前半變綠，而那比原本的缺陷更糟。
    """
    rounds = [(40.0, 0.0, RC_ZERO_PROGRESS)] * 5
    rc, clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, list(rounds), rapid_giveup=99,
        zero_progress_giveup=2)
    assert (rc, spawns) == (1, 2), (
        f"連續兩輪零產出之後應該放棄（rc=1、spawn 2 次），實際 rc={rc}、"
        f"spawn {spawns} 次。生成被擋住時每一輪都要跑完 consecutive_fail_abort "
        "才結束，遠遠超過 rapid_fail_threshold_sec——那道閘永遠不會響。")
    assert clock.slept, "放棄之前那一輪的退避沒有睡"

    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path, list(rounds), rapid_giveup=99,
        zero_progress_giveup=99)
    assert (rc, spawns) == (None, 6), (
        f"門檻調高之後就該繼續重生（rc={rc}、spawn {spawns} 次）。少了這一半，"
        "把這道閘寫成「永遠放棄」也會全綠。")


def test_a_good_run_clears_the_zero_progress_counter(monkeypatch, tmp_path):
    """計數要求的是**連續**——中間插一輪有產出就要歸零。

    不歸零的話，一台跑了好幾天、偶爾出現單輪零產出的機器會慢慢累積到門檻，然後
    在完全正常的時候停下來。
    """
    rc, _clock, spawns = _supervise_with_clock(
        monkeypatch, tmp_path,
        [(40.0, 0.0, RC_ZERO_PROGRESS), (40.0, 0.0, 1),
         (40.0, 0.0, RC_ZERO_PROGRESS), (40.0, 0.0, 0)],
        rapid_giveup=99, zero_progress_giveup=2)
    assert (rc, spawns) == (0, 4), (
        f"中間那一輪不是零產出，計數卻沒有歸零（rc={rc}、spawn {spawns} 次）。")


def test_a_backoff_config_with_max_below_min_is_refused_before_anything_spawns(
        monkeypatch, tmp_path):
    """`max < min` 要在啟動時就擋下來，不能留到第一次崩潰才炸。

    `_bot_config._coerce_supervisor` 只保證每個值各自 > 0，所以這種設定載得進來、
    啟動得起來，然後在**第一次崩潰時**讓 `restart_backoff` 丟 `ValueError` 把監督者
    整支帶走——正好是它該接手的那一刻。訊息要同時點名兩個 key，不然使用者只知道
    「設定壞了」卻不知道壞在哪一對。
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)
    monkeypatch.setattr(sys, "argv", [WEBRUNNER_LAUNCHER])
    monkeypatch.setattr(module, "LOCK_FILE", tmp_path / "test.lock")
    monkeypatch.setattr(module, "WEBRUNNER_LOG", tmp_path / "webrunner.log")
    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE",
                        tmp_path / "webrunner.pid")
    monkeypatch.setattr(module, "_chrome_slot", _FakeSlot())
    monkeypatch.setattr(module, "acquire_single_instance_lock",
                        lambda _path: _SpyLock())
    monkeypatch.setattr(module, "load_bot_config", lambda: {
        "webrunner_supervisor": {
            "respawn_backoff_min_sec": 300,
            "respawn_backoff_max_sec": 5,       # 反過來了
            "healthy_threshold_sec": 60,
            "rapid_fail_threshold_sec": 30,
            "rapid_fail_giveup_count": 3,
            "zero_progress_giveup_count": 3,
        }})

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("設定是壞的，卻還是進了監督迴圈")

    monkeypatch.setattr(module, "_supervise", _must_not_run)
    monkeypatch.setattr(module, "stream_child", _must_not_run)

    assert module.main() == 1, "壞掉的退避設定應該讓啟動器以 rc=1 收工"


def test_a_pid_file_deleted_mid_read_counts_as_no_batch(monkeypatch, tmp_path):
    """`exists()` 與 `read_text()` 之間被刪掉：那等同「檔案不存在」，可以啟動。

    這條要跟「讀不出來」分清楚。合在一起走保守那邊的話，一次剛好撞上收尾的競態
    就會讓啟動器拒絕啟動，而使用者手上那個 pid 檔早就不見了——沒有任何線索。
    """
    module = _load_launcher(WEBRUNNER_LAUNCHER)

    class _VanishingPidFile:
        def exists(self):
            return True

        def read_text(self, *_args, **_kwargs):
            raise FileNotFoundError("剛好在這一瞬間被收尾刪掉")

    monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", _VanishingPidFile())
    assert module._live_webrunner_pid() == (None, True), (
        "檔案在讀之前就被刪掉了，那是**判定得出來**的「沒有批次在跑」，"
        "不是「判不出來」。")


if __name__ == "__main__":
    sys.exit(main())
