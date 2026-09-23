"""隔離驗證那條路——`verify_browser.py --full` 真正在跑的那個子行程。

`CLAUDE.md` 的驗證守則規定：動到瀏覽器／driver／Selenium 啟動路徑，就要用
`verify_browser.py` 實證。`--full` 做的事是開一個子行程跑
`webrunner_novelai._run_setup_verification()`——登入、導航、確認產圖介面可達，必要時
在隔離目錄真的產一張用完即丟的圖。也就是說：**這五支函式是這個專案「我驗過了」這句話
的全部依據**，而量起來它們幾乎一行都沒被跑過（65＋20＋27＋11＋21 ＝ 144 行敘述）。
全樹提到 `_run_setup_verification` 的四個地方全是 AST 靜態檢查——檢查它的**形狀**，
沒有一個真的呼叫過它。

這正是這個 repo 記過好幾次的形狀：`verify_browser.run_full` 那次的 cp950 缺陷之所以
能躲過整輪清查，就是因為**工具讀起來是健康的**——那行 `VERIFY-BROWSER: OK` 是純
ASCII，活得好好的，壞掉的是旁邊的診斷。一個驗證工具最糟的失效不是壞掉，是**看起來
過了**。

#### 這一檔釘的四件事

**一、rc 與結果行是一對。** `verify_browser.py` 同時要求「子行程印了
`VERIFY-SETUP: OK`」**且**「rc == 0」，兩者缺一就判失敗（它甚至為「印了 OK 卻 rc 非 0」
留了一句專門的訊息）。所以每一條失敗路徑都必須恰好印一行 `FAIL`、不得印 `OK`，而
成功那條必須兩者都成立。這條是用推導的方式釘的——對每一種注入的故障跑一次，檢查
這個不變式，而不是各寫各的字面斷言。

**二、隔離保證是安全性的，不是整潔。** 這台機器上同時跑著正式批次的 Chrome。若
`_SUPPRESS_ORPHAN_SWEEP` 在開 driver **之前**沒有設起來，那次驗證會把整台機器上的
chrome.exe 掃光，中斷一個已經跑了幾十小時的批次。同理，`CHROME_PROFILE_SNAPSHOT`
若沒有先指到隔離目錄，驗證就會去寫正式批次用的快照目錄。所以這裡的斷言量的是
**呼叫 `build_stealth_driver()` 的那一刻**那兩個全域變數的值，不是函式跑完之後的值
——跑完才對是沒有用的。

**三、回收要先對身分。** `_verify_reap_tree` 在 `driver.quit()` **之後**才殺
——而 quit 會讓幾十個 chrome 子行程結束、把 PID 釋放回池子。拿裸 pid 去殺，殺的是
**現在**那個 pid 的擁有者。判準是 `(pid, create_time)`：對不上、或當初根本沒抓到
（`None`），一律跳過。「不確定就不殺」在這裡是硬要求，因為誤殺的代價是一個正式批次。

**四、清理跑在 `finally`。** 隔離 profile 是**正式登入態的複本**——`.gitignore` 曾經
沒蓋到它（`CLAUDE.md` 的 git 段記著這件事）。任何一條失敗路徑忘了刪，那份複本就留在
磁碟上。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import webrunner_novelai as wn  # noqa: E402
import _webrunner_shared as ws  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


# ---------------------------------------------------------------------------
# 夾具
# ---------------------------------------------------------------------------

class _FakeDriver:
    def __init__(self, env, pid: int = 4321):
        self._env = env
        self.service = types.SimpleNamespace(process=types.SimpleNamespace(pid=pid))

    def quit(self):
        self._env.quit_calls += 1
        if self._env.quit_raises:
            raise RuntimeError("quit 失敗")


@pytest.fixture
def vfy(monkeypatch, tmp_path):
    """把整條路上每一個會碰到外界的東西換成替身，並記錄**呼叫當下**的全域狀態。"""
    env = types.SimpleNamespace(
        credentials_raise=None, spawn_raises=None, setup_ok=True,
        areas=["#prompt"], gen_btn=object(), generate_result=(True, "verify.png"),
        capture_raises=False, quit_raises=False, tree={4321: 111.0},
        reaped_count=0,
        # 記錄
        spawn_calls=0, sweep_at_spawn=None, snapshot_at_spawn=None,
        quit_calls=0, reap_args=[], rmtree=[], setup_args=[], generate_args=[],
        capture_args=[], sync_back_calls=0,
    )

    def fake_read_credentials(path):
        if env.credentials_raise is not None:
            raise env.credentials_raise
        return ("someone@example.invalid", "pw")

    def fake_build():
        env.spawn_calls += 1
        # **在這一刻**量——跑完才對是沒有用的。
        env.sweep_at_spawn = wn._SUPPRESS_ORPHAN_SWEEP
        env.snapshot_at_spawn = wn.CHROME_PROFILE_SNAPSHOT
        if env.spawn_raises is not None:
            raise env.spawn_raises
        return _FakeDriver(env)

    def fake_capture(pid):
        env.capture_args.append(pid)
        if env.capture_raises:
            raise RuntimeError("抓不到行程樹")
        return dict(env.tree)

    def fake_setup(driver, email, password):
        env.setup_args.append((email, password))
        return env.setup_ok

    def fake_reap(tree):
        env.reap_args.append(dict(tree))
        return env.reaped_count

    def fake_rmtree(path):
        env.rmtree.append(Path(path))

    def fake_generate(driver, output_dir):
        env.generate_args.append(Path(output_dir))
        return env.generate_result

    def fake_sync_back(*_a, **_k):
        env.sync_back_calls += 1
        raise AssertionError("隔離驗證**絕對不可以**把 profile 同步回正式登入態")

    monkeypatch.setattr(wn, "read_credentials", fake_read_credentials)
    monkeypatch.setattr(wn, "build_stealth_driver", fake_build)
    monkeypatch.setattr(wn, "_verify_capture_tree", fake_capture)
    monkeypatch.setattr(wn, "_setup_session", fake_setup)
    monkeypatch.setattr(wn, "_verify_reap_tree", fake_reap)
    monkeypatch.setattr(wn, "_verify_rmtree", fake_rmtree)
    monkeypatch.setattr(wn, "_verify_generate_one", fake_generate)
    monkeypatch.setattr(wn, "_sync_chrome_profile_back", fake_sync_back,
                        raising=False)
    monkeypatch.setattr(wn, "webdriver_wrapper_instance",
                        types.SimpleNamespace(current_webdriver=None))
    monkeypatch.setattr(ws, "find_prompt_areas", lambda _p: env.areas)
    monkeypatch.setattr(ws, "find_generate_button", lambda _p: env.gen_btn)

    # 全域狀態：monkeypatch 會在測試結束時還原，函式自己的 `global` 指派不會外洩。
    monkeypatch.setattr(wn, "_SUPPRESS_ORPHAN_SWEEP", False)
    monkeypatch.setattr(wn, "CHROME_PROFILE_SNAPSHOT",
                        Path("D:/絕對不該被寫到的正式快照"))
    monkeypatch.setattr(wn, "OUTPUT_ROOT", tmp_path / "output")

    for name in (wn.VERIFY_GENERATE_ENV, wn.VERIFY_PROFILE_DEST_ENV,
                 wn.VERIFY_OUTPUT_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    env.profile_dest = tmp_path / "隔離profile"
    monkeypatch.setenv(wn.VERIFY_PROFILE_DEST_ENV, str(env.profile_dest))
    env.tmp = tmp_path
    return env


def _result_lines(capsys) -> list[str]:
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("VERIFY-SETUP:")]


# ---------------------------------------------------------------------------
# 一、rc 與結果行是一對
# ---------------------------------------------------------------------------

def _break(env, how: str) -> None:
    """把 `env` 弄成指定的故障。每一種對應 `_run_setup_verification` 的一個出口。"""
    if how == "ok":
        return
    if how == "credentials":
        env.credentials_raise = OSError("憑證檔讀不到")
    elif how == "spawn":
        env.spawn_raises = RuntimeError("driver 起不來")
    elif how == "login":
        env.setup_ok = False
    elif how == "prompt_area":
        env.areas = []
    elif how == "generate_button":
        env.gen_btn = None
    elif how == "generate":
        env.generate_result = (False, "影像下載失敗")
    else:  # pragma: no cover - 參數表以外的值是測試自己寫錯
        raise AssertionError(how)


_EXITS = [
    ("ok", 0),
    ("credentials", 2),
    ("spawn", 1),
    ("login", 4),
    ("prompt_area", 5),
    ("generate_button", 6),
    ("generate", 7),
]


@pytest.mark.parametrize("how, rc", _EXITS, ids=[c[0] for c in _EXITS])
def test_each_exit_returns_its_own_code(vfy, capsys, monkeypatch, how, rc):
    """七個出口、七個不同的 rc。

    `verify_browser.py` 把 rc 原樣轉給人看；全部壓成 1 的話，「登入態過期」與
    「driver 起不來」在畫面上長得一模一樣，而這兩件事要做的處置完全不同。
    """
    if how == "generate":
        monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    _break(vfy, how)
    assert wn._run_setup_verification() == rc


@pytest.mark.parametrize("how, rc", _EXITS, ids=[c[0] for c in _EXITS])
def test_the_result_line_and_the_exit_code_always_agree(vfy, capsys, monkeypatch,
                                                        how, rc):
    """**本檔最重要的不變式。**

    `verify_browser.py` 要求「印了 `VERIFY-SETUP: OK`」**且**「rc == 0」，兩者缺一
    就判失敗——它甚至為「印了 OK 卻 rc 非 0」留了一句專門的訊息，可見這個配對曾經
    被認為有可能走岔。這裡用推導的方式釘：每一種故障各跑一次，檢查同一條不變式，
    而不是各寫各的字面斷言。
    """
    if how == "generate":
        monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    _break(vfy, how)
    actual = wn._run_setup_verification()
    lines = _result_lines(capsys)
    assert len(lines) == 1, f"結果行不是恰好一行：{lines}"
    said_ok = lines[0].startswith("VERIFY-SETUP: OK")
    assert said_ok is (actual == 0), (
        f"rc={actual} 與結果行 {lines[0]!r} 對不起來")
    assert actual == rc


def test_a_failure_reason_is_carried_on_the_same_line(vfy, capsys):
    """原因要跟在 `FAIL` 後面同一行——`verify_browser._reason_from_child_output`
    是逐行掃、只認開頭是 `VERIFY-SETUP: FAIL` 的那一行。換行就等於沒有原因。"""
    vfy.setup_ok = False
    wn._run_setup_verification()
    line = _result_lines(capsys)[0]
    assert line.startswith("VERIFY-SETUP: FAIL")
    assert line[len("VERIFY-SETUP: FAIL"):].strip(), "FAIL 後面是空的"


def test_a_driver_that_will_not_start_reports_the_full_message(vfy, capsys):
    """spawn 失敗走 `full_error_detail`，不是 `!r`。

    selenium 的 `WebDriverException` 把訊息存在 `self.msg`、`args` 是空的，所以
    `repr()` 印出來只剩一對空括號——而這條路是**隔離驗證的鑑識出口**：跑一次、不在
    任何迴圈裡、輸出由人直接讀。訊息沒了就什麼也查不到。
    """
    class _MsgOnly(Exception):
        def __init__(self, msg):
            super().__init__()
            self.msg = msg

        def __str__(self):
            return self.msg

    vfy.spawn_raises = _MsgOnly("session not created: 版本對不上")
    assert wn._run_setup_verification() == 1
    line = _result_lines(capsys)[0]
    assert "版本對不上" in line, f"完整訊息不見了：{line!r}"


# ---------------------------------------------------------------------------
# 二、隔離保證（安全性）
# ---------------------------------------------------------------------------

def test_the_orphan_sweep_is_suppressed_before_the_driver_is_started(vfy):
    """**這條斷言的代價是一個正在跑的正式批次。**

    這台機器上同時跑著正式批次的 Chrome。`build_stealth_driver` 那條路會做全機
    chrome.exe 掃光；旗標若沒有在**開 driver 之前**設起來，一次驗證就會中斷一個
    已經跑了幾十小時的批次。所以量的是**呼叫的那一刻**——跑完才對是沒有用的。
    """
    wn._run_setup_verification()
    assert vfy.spawn_calls == 1
    assert vfy.sweep_at_spawn is True, (
        "開 driver 的時候 `_SUPPRESS_ORPHAN_SWEEP` 還不是 True——這一次驗證會把"
        "整台機器上的 chrome.exe 掃光")


def test_the_profile_snapshot_points_at_the_isolated_dir_before_the_driver_starts(vfy):
    """快照目的地同樣要在開 driver **之前**就指到隔離目錄，否則驗證會去寫正式
    批次用的那一份。"""
    wn._run_setup_verification()
    assert vfy.snapshot_at_spawn == vfy.profile_dest


def test_the_isolated_destination_honours_the_environment_variable(vfy, monkeypatch):
    elsewhere = vfy.tmp / "別的地方"
    monkeypatch.setenv(wn.VERIFY_PROFILE_DEST_ENV, str(elsewhere))
    wn._run_setup_verification()
    assert vfy.snapshot_at_spawn == elsewhere


def test_without_the_environment_variable_it_still_never_uses_the_production_snapshot(
        vfy, monkeypatch):
    """預設值也必須是隔離的。`.chrome_profile_snap` 是正式批次在用的那一份。"""
    monkeypatch.delenv(wn.VERIFY_PROFILE_DEST_ENV, raising=False)
    wn._run_setup_verification()
    assert vfy.snapshot_at_spawn is not None
    assert vfy.snapshot_at_spawn.name != ".chrome_profile_snap"
    assert "verify" in vfy.snapshot_at_spawn.name


def test_the_login_profile_is_never_synced_back(vfy):
    """正式登入態是唯讀的輸入。同步回去等於讓一次驗證改寫掉正式的登入 profile。

    替身會 raise，所以真的被呼叫時這支會紅——而不是安靜地記一個數字然後在最後
    才斷言（那種寫法在「呼叫發生在某個 `except` 裡被吞掉」時會失效）。
    """
    wn._run_setup_verification()
    assert vfy.sync_back_calls == 0


_EXITS_THAT_STARTED_A_DRIVER = [c for c in _EXITS if c[0] != "credentials"]


@pytest.mark.parametrize("how, _rc", _EXITS_THAT_STARTED_A_DRIVER,
                         ids=[c[0] for c in _EXITS_THAT_STARTED_A_DRIVER])
def test_the_isolated_profile_is_removed_on_every_path_that_created_one(
        vfy, monkeypatch, how, _rc):
    """隔離 profile 是**正式登入態的複本**。任何一條路忘了刪，那份複本就留在磁碟上
    ——而 `.gitignore` 曾經沒蓋到它（`CLAUDE.md` 的 git 段記著這件事）。

    包含 `spawn` 那一條：driver 起不來時 `build_stealth_driver` 可能已經把 profile
    複製完才失敗，所以「沒有 driver 物件」不等於「沒有東西要刪」。
    """
    if how == "generate":
        monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    _break(vfy, how)
    wn._run_setup_verification()
    assert vfy.profile_dest in vfy.rmtree, (
        f"`{how}` 這條路沒有刪掉隔離 profile：{vfy.rmtree}")


def test_the_credentials_path_returns_before_anything_is_created(vfy):
    """讀憑證失敗是唯一**不經過**清理 `finally` 的出口，而那是對的。

    這條路在 `try` 之前就 return，所以清理不會跑——但它同樣還沒呼叫
    `build_stealth_driver`，而複製登入 profile 的正是那一支。沒有東西被建立，也就
    沒有東西要刪。第一次寫這支測試時我把它一起放進「每一條路都要刪」的參數表，
    紅了；查下去的結論是**測試錯了，不是程式錯了**，所以把前提寫在這裡而不是放寬
    那張表。

    它也不能搬進 `try` 去「順便」享受 `finally`：那會讓它落進最外層的
    `except Exception` 而回 rc=1，於是「憑證讀不到」與「driver 起不來」在
    `verify_browser.py` 的輸出裡變成同一件事，而這兩者要做的處置完全不同。
    """
    vfy.credentials_raise = OSError("憑證檔讀不到")
    assert wn._run_setup_verification() == 2
    assert vfy.spawn_calls == 0, "憑證都還沒讀到就去開 driver 了"
    assert vfy.rmtree == []
    assert vfy.reap_args == []


def test_the_isolated_output_dir_is_only_removed_when_it_was_used(vfy, monkeypatch):
    """沒開 `--generate` 就沒有輸出目錄；照刪會刪到一個不屬於這次驗證的東西。"""
    wn._run_setup_verification()
    assert len(vfy.rmtree) == 1, f"多刪了東西：{vfy.rmtree}"

    vfy.rmtree.clear()
    monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    wn._run_setup_verification()
    assert len(vfy.rmtree) == 2, f"開了 generate 卻沒刪輸出目錄：{vfy.rmtree}"


def test_the_isolated_output_dir_honours_its_environment_variable(vfy, monkeypatch):
    target = vfy.tmp / "隔離輸出"
    monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    monkeypatch.setenv(wn.VERIFY_OUTPUT_DIR_ENV, str(target))
    wn._run_setup_verification()
    assert vfy.generate_args == [target]
    assert target in vfy.rmtree


def test_generation_only_happens_when_it_was_asked_for(vfy, monkeypatch):
    """預設不產圖。`--full` 不帶 `--generate` 時真的去產一張，等於每次驗證都消耗
    一次額度，而那正是這條路最想避免的副作用。"""
    wn._run_setup_verification()
    assert vfy.generate_args == []

    monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, "1")
    wn._run_setup_verification()
    assert len(vfy.generate_args) == 1


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
def test_only_the_exact_flag_value_turns_generation_on(vfy, monkeypatch, value):
    """判準是 `== "1"`。任何其他值都不算——包括看起來像「開」的字。"""
    monkeypatch.setenv(wn.VERIFY_GENERATE_ENV, value)
    wn._run_setup_verification()
    assert vfy.generate_args == []


# ---------------------------------------------------------------------------
# 三、收尾
# ---------------------------------------------------------------------------

def test_the_driver_is_quit_and_its_own_tree_is_reaped(vfy):
    wn._run_setup_verification()
    assert vfy.quit_calls == 1
    assert vfy.reap_args == [{4321: 111.0}]


def test_the_tree_is_captured_before_quit_not_after(vfy, monkeypatch):
    """抓行程樹必須在 `quit()` **之前**——quit 會讓幾十個 chrome 子行程結束，之後
    再抓就只剩空的，那些被 reparent 的殘留就沒人回收了。"""
    order = []

    class _Recording(_FakeDriver):
        def quit(self):
            order.append("quit")
            return super().quit()

    monkeypatch.setattr(wn, "_verify_capture_tree",
                        lambda _pid: (order.append("capture"), dict(vfy.tree))[1])
    monkeypatch.setattr(wn, "build_stealth_driver", lambda: _Recording(vfy))
    wn._run_setup_verification()
    assert order == ["capture", "quit"], order


def test_a_non_empty_reap_is_reported_as_progress(vfy, capsys):
    """回收到殘留要出聲。這條路是**事後判讀**用的：`verify_browser.py` 的輸出由人
    或 agent 直接讀，「這次留了幾個行程下來」是唯一看得到的線索。"""
    vfy.reaped_count = 3
    wn._run_setup_verification()
    out = capsys.readouterr().out
    assert "3" in out and "清理" in out


def test_a_failed_capture_does_not_stop_the_verification(vfy, capsys):
    """抓不到行程樹是「少回收一點」，不是「驗證失敗」。把它變成失敗，等於讓一個
    無害的權限問題否決掉整次驗證。"""
    vfy.capture_raises = True
    assert wn._run_setup_verification() == 0
    assert vfy.reap_args == [{}], "抓不到的時候應該拿空的樹去回收"


def test_a_driver_that_refuses_to_quit_does_not_change_the_verdict(vfy, capsys):
    """`quit()` 在 driver 已經死掉時很常丟例外。讓它翻掉整個結果，就會把一次成功
    的驗證報成失敗。"""
    vfy.quit_raises = True
    assert wn._run_setup_verification() == 0
    assert _result_lines(capsys)[0].startswith("VERIFY-SETUP: OK")


def test_the_cleanup_runs_even_when_the_driver_never_started(vfy):
    """spawn 就失敗時沒有 driver 可以 quit，但清理照樣要跑完。"""
    vfy.spawn_raises = RuntimeError("起不來")
    assert wn._run_setup_verification() == 1
    assert vfy.quit_calls == 0
    assert vfy.reap_args == [{}]
    assert vfy.profile_dest in vfy.rmtree


def test_the_current_driver_is_published_before_setup_runs(vfy, monkeypatch):
    """共用的 DOM 輔助函式是透過 `webdriver_wrapper_instance` 取得 driver 的；
    沒設、或設得太晚，整條 setup 就是對著一個 None 操作。"""
    seen = []
    monkeypatch.setattr(
        wn, "_setup_session",
        lambda driver, email, password: (
            seen.append(wn.webdriver_wrapper_instance.current_webdriver), True)[1])
    wn._run_setup_verification()
    assert seen and seen[0] is not None, "setup 開始跑的時候 driver 還沒發佈出去"


# ---------------------------------------------------------------------------
# 四、`_verify_capture_tree`
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, pid, born, kids=(), born_raises=False):
        self.pid = pid
        self._born = born
        self._kids = list(kids)
        self._born_raises = born_raises
        self.killed = 0

    def create_time(self):
        if self._born_raises:
            raise RuntimeError("讀不到建立時間")
        return self._born

    def children(self, recursive=False):
        return list(self._kids)

    def kill(self):
        self.killed += 1


def _fake_psutil(monkeypatch, procs: dict, missing=()):
    """把 `sys.modules["psutil"]` 換成一個只實作 `Process` 的假模組。"""
    class _NoSuchProcess(Exception):
        pass

    def _process(pid):
        if pid in missing or pid not in procs:
            raise _NoSuchProcess(pid)
        return procs[pid]

    module = types.SimpleNamespace(Process=_process, NoSuchProcess=_NoSuchProcess)
    monkeypatch.setitem(sys.modules, "psutil", module)
    return module


def _without_psutil(monkeypatch):
    """模擬「沒有裝 psutil」。

    ※ 用 `= None` 而不是 `pop`：把它從 `sys.modules` 拿掉只會讓下一次 `import`
    重新載入**真的** psutil（這個 repo 為此付過代價），塞 None 才會讓
    `import psutil` 丟 `ImportError`。
    """
    monkeypatch.setitem(sys.modules, "psutil", None)


def test_capturing_without_a_service_pid_yields_nothing():
    assert wn._verify_capture_tree(None) == {}
    assert wn._verify_capture_tree(0) == {}


def test_capturing_records_the_parent_and_every_descendant(monkeypatch):
    kids = [_FakeProc(21, 202.0), _FakeProc(22, 203.0)]
    procs = {11: _FakeProc(11, 201.0, kids)}
    _fake_psutil(monkeypatch, procs)
    assert wn._verify_capture_tree(11) == {11: 201.0, 21: 202.0, 22: 203.0}


def test_a_child_whose_identity_cannot_be_read_is_recorded_as_unknown(monkeypatch):
    """身分讀不到要記成 `None`，不是**漏掉**。漏掉的話 `_verify_reap_tree` 連
    「跳過」的機會都沒有，那個 pid 就完全不在收尾的視野裡了。"""
    kids = [_FakeProc(21, None, born_raises=True)]
    _fake_psutil(monkeypatch, {11: _FakeProc(11, 201.0, kids)})
    assert wn._verify_capture_tree(11) == {11: 201.0, 21: None}


def test_a_parent_whose_identity_cannot_be_read_still_yields_its_pid(monkeypatch):
    _fake_psutil(monkeypatch, {11: _FakeProc(11, None, born_raises=True)})
    assert wn._verify_capture_tree(11) == {11: None}


def test_a_service_pid_that_is_already_gone_still_yields_its_own_entry(monkeypatch):
    _fake_psutil(monkeypatch, {}, missing=(11,))
    assert wn._verify_capture_tree(11) == {11: None}


def test_without_psutil_the_capture_degrades_to_the_bare_pid(monkeypatch):
    _without_psutil(monkeypatch)
    assert wn._verify_capture_tree(11) == {11: None}


# ---------------------------------------------------------------------------
# 五、`_verify_reap_tree`——「不確定就不殺」
# ---------------------------------------------------------------------------

def test_reaping_an_empty_tree_kills_nothing():
    assert wn._verify_reap_tree({}) == 0


def test_a_matching_identity_is_killed(monkeypatch):
    proc = _FakeProc(11, 201.0)
    _fake_psutil(monkeypatch, {11: proc})
    assert wn._verify_reap_tree({11: 201.0}) == 1
    assert proc.killed == 1


def test_a_recycled_pid_is_never_killed(monkeypatch):
    """**本節的重點。** `driver.quit()` 會釋放幾十個 PID 回池子，而回收與重發之間
    隔著的就是那一步。建立時間對不上 ＝ 這個 pid 已經是別人的了。

    這台機器同時跑著兩個正式批次的 Chrome，誤殺等於中斷一個跑了幾十小時的批次。
    """
    intruder = _FakeProc(11, 999.0)   # 同一個 pid，不同的行程
    _fake_psutil(monkeypatch, {11: intruder})
    assert wn._verify_reap_tree({11: 201.0}) == 0
    assert intruder.killed == 0, "殺了一個不是我們開的行程"


def test_an_unknown_identity_is_never_killed(monkeypatch):
    """capture 當下就沒抓到身分（`None`）的，照樣不殺。

    留下一個帶著隔離 profile 的 orphan 是無害的——`_verify_rmtree` 本來就容忍殘留
    ；殺錯行程不是。
    """
    proc = _FakeProc(11, 201.0)
    _fake_psutil(monkeypatch, {11: proc})
    assert wn._verify_reap_tree({11: None}) == 0
    assert proc.killed == 0


def test_one_unkillable_process_does_not_stop_the_rest(monkeypatch):
    class _Stubborn(_FakeProc):
        def kill(self):
            raise PermissionError("不給殺")

    good = _FakeProc(22, 202.0)
    _fake_psutil(monkeypatch, {11: _Stubborn(11, 201.0), 22: good})
    assert wn._verify_reap_tree({11: 201.0, 22: 202.0}) == 1
    assert good.killed == 1


def test_without_psutil_it_falls_back_to_a_per_pid_taskkill(monkeypatch):
    """退路仍然只動指定的 pid。`taskkill /IM chrome.exe` 那種全殺是明令禁止的
    ——它會連正式批次的 Chrome 一起殺掉。"""
    _without_psutil(monkeypatch)
    monkeypatch.setattr(os, "name", "nt")
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert wn._verify_reap_tree({11: 201.0, 22: None}) == 2
    assert [c[:4] for c in calls] == [["taskkill", "/F", "/T", "/PID"]] * 2
    assert sorted(c[4] for c in calls) == ["11", "22"]
    for call in calls:
        assert "/IM" not in call, "退路用了全殺"


def test_the_taskkill_fallback_counts_only_what_it_actually_killed(monkeypatch):
    _without_psutil(monkeypatch)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "run",
                        lambda *_a, **_k: types.SimpleNamespace(returncode=128))
    assert wn._verify_reap_tree({11: 201.0}) == 0


def test_the_taskkill_fallback_survives_a_failing_call(monkeypatch):
    _without_psutil(monkeypatch)
    monkeypatch.setattr(os, "name", "nt")

    def _boom(*_a, **_k):
        raise OSError("taskkill 不在 PATH 上")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert wn._verify_reap_tree({11: 201.0}) == 0


# ---------------------------------------------------------------------------
# 六、`_verify_rmtree`
# ---------------------------------------------------------------------------

def test_removing_an_absent_directory_is_a_no_op(tmp_path, monkeypatch):
    """不存在就立刻回來。

    ※ 不能只斷言「沒有丟例外」——把 `if not path.exists(): return` 拿掉之後結果
    一樣是「沒有丟例外」，只是先重試六次、睡了三秒才由 `ignore_errors` 兜底。而
    清理跑在 `verify_browser.py` 的總期限裡面，三秒是真的成本。所以這裡斷言的是
    **它沒有去睡**。
    """
    monkeypatch.setattr(wn.time, "sleep", lambda _s: (_ for _ in ()).throw(
        AssertionError("為了一個不存在的目錄睡了")))
    wn._verify_rmtree(tmp_path / "不存在")


def test_a_directory_is_actually_removed(tmp_path):
    target = tmp_path / "隔離"
    (target / "深" / "一點").mkdir(parents=True)
    (target / "深" / "一點" / "檔案.txt").write_text("x", encoding="utf-8")
    wn._verify_rmtree(target)
    assert not target.exists()


def test_a_locked_directory_is_retried_then_given_up_on(tmp_path, monkeypatch):
    """Chrome 剛被殺掉時還握著 handle，所以要重試。但**不能無限重試**——
    `verify_browser.py` 有總期限，卡在清理等於把一次成功的驗證拖成逾時。
    """
    target = tmp_path / "鎖住的"
    target.mkdir()
    attempts = []
    import shutil

    def _fake_rmtree(path, ignore_errors=False):
        attempts.append(bool(ignore_errors))
        if not ignore_errors:
            raise OSError("檔案被佔用")

    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(wn.time, "sleep", lambda _s: None)
    wn._verify_rmtree(target)
    assert attempts.count(False) == 6, f"重試次數變了：{attempts}"
    assert attempts[-1] is True, "最後一次應該是 ignore_errors 的兜底"


def test_the_retry_loop_stops_as_soon_as_it_succeeds(tmp_path, monkeypatch):
    """必須放行的那一面：一次就成功時不該再呼叫第二次，也不該去睡。"""
    target = tmp_path / "好清的"
    target.mkdir()
    calls = []
    import shutil

    monkeypatch.setattr(shutil, "rmtree",
                        lambda path, ignore_errors=False: calls.append(path))
    monkeypatch.setattr(wn.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(
                            AssertionError("成功了還去睡")))
    wn._verify_rmtree(target)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 七、`_verify_generate_one`——真的產一張，但什麼正式狀態都不准碰
# ---------------------------------------------------------------------------

@pytest.fixture
def gen(monkeypatch, tmp_path):
    """`--generate` 那條路的替身。每一支測試只破壞其中一環。"""
    env = types.SimpleNamespace(
        fill_ok=True, previous_src="舊圖", new_src="新圖",
        blocked=False, download_ok=True, write_file=True, file_bytes=b"PNG",
        prompts=[], dismissed=0, download_args=[], retry_args=[],
        out=tmp_path / "隔離輸出",
    )

    def fake_with_retry(name, fn, **kwargs):
        env.retry_args.append((name, kwargs))
        return fn() if env.fill_ok else False

    def fake_fill(port_, prompt):
        env.prompts.append(prompt)
        return True

    def fake_generate(port_, previous, **kwargs):
        if env.blocked:
            raise ws.GenerationBlockedError("額度用完")
        return env.new_src

    def fake_download(port_, src, target, **kwargs):
        env.download_args.append((src, Path(target), kwargs))
        if not env.download_ok:
            return False
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        if env.write_file:
            Path(target).write_bytes(env.file_bytes)
        return True

    monkeypatch.setattr(wn, "load_batch_config", lambda: {
        "generate_max_retries": 5, "generate_retry_delay_sec": 7,
        "download_max_retries": 9})
    monkeypatch.setattr(wn, "human_pause", lambda *_a, **_k: None)
    monkeypatch.setattr(ws, "with_retry", fake_with_retry)
    monkeypatch.setattr(ws, "fill_main_prompt", fake_fill)
    monkeypatch.setattr(ws, "get_main_image_src", lambda _p: env.previous_src)
    monkeypatch.setattr(ws, "generate_one_image", fake_generate)
    monkeypatch.setattr(ws, "download_image_with_retry", fake_download)
    monkeypatch.setattr(
        ws, "dismiss_blocking_dialog",
        lambda _p: env.__setattr__("dismissed", env.dismissed + 1))
    return env


def test_one_image_lands_in_the_isolated_directory(gen):
    ok, detail = wn._verify_generate_one(object(), gen.out)
    assert ok, detail
    written = list(gen.out.glob("*.png"))
    assert len(written) == 1, f"產出的檔案不是剛好一張：{written}"
    assert written[0].name in detail and str(len(gen.file_bytes)) in detail


def test_the_output_directory_is_created_if_it_is_not_there(gen):
    assert not gen.out.exists()
    assert wn._verify_generate_one(object(), gen.out)[0]
    assert gen.out.is_dir()


def test_a_prompt_that_cannot_be_filled_stops_before_generating(gen):
    """填不進去就不要按 Generate——按下去會用上一次留在畫面上的 prompt 產圖，
    那張圖與這次驗證無關，卻照樣消耗一次額度。"""
    gen.fill_ok = False
    ok, detail = wn._verify_generate_one(object(), gen.out)
    assert (ok, detail) == (False, "無法填入隔離 prompt")
    assert gen.download_args == []


def test_a_blocked_generation_is_reported_not_waited_out(gen):
    """額度對話框：**不等**。

    `verify_browser.py` 有 240 秒的總期限，而正式批次遇到這個對話框的做法是睡到
    額度回來——那條路在這裡會直接撞破期限，結果是一次「驗證逾時」，而真正的原因
    （額度不足）完全看不到。順手把對話框關掉，免得它留在畫面上干擾同一個 session
    後面的檢查。
    """
    gen.blocked = True
    ok, detail = wn._verify_generate_one(object(), gen.out)
    assert ok is False
    assert "額度" in detail, detail
    assert gen.dismissed == 1, "對話框沒有被關掉，會留在畫面上"


def test_an_image_that_never_changed_is_a_failure(gen):
    gen.new_src = None
    ok, detail = wn._verify_generate_one(object(), gen.out)
    assert (ok, detail) == (False, "Generate 後影像未更新（重試耗盡）")


def test_a_failed_download_is_a_failure(gen):
    gen.download_ok = False
    assert wn._verify_generate_one(object(), gen.out) == (False, "影像下載失敗")


def test_a_download_that_reports_success_but_wrote_nothing_is_a_failure(gen):
    """下載函式回 True 不等於磁碟上真的有東西。

    這是本專案反覆記過的形狀：把「回傳值」當成「結果」。驗證的整個意義就是
    確認那張圖真的落地了，所以這裡一定要自己看檔案。
    """
    gen.write_file = False
    assert wn._verify_generate_one(object(), gen.out) == (False, "影像檔未生成或為空")


def test_a_zero_byte_image_is_a_failure(gen):
    gen.file_bytes = b""
    assert wn._verify_generate_one(object(), gen.out) == (False, "影像檔未生成或為空")


def test_the_prompt_is_a_throwaway_literal_not_the_real_queue(gen):
    """用的是寫死的拋棄式 prompt。改成去讀 `todo_prompt.md` 會同時踩兩件事：
    驗證的結果變得不可重現，而且那條路是**只讀不寫**佇列的硬要求的反面教材。"""
    wn._verify_generate_one(object(), gen.out)
    assert gen.prompts and gen.prompts[0].strip(), "沒有填入任何 prompt"


def test_the_retry_budget_comes_from_the_batch_config(gen):
    """重試次數不要另寫一份。寫死的話，擁有者調了批次設定卻發現驗證行為不一樣，
    而兩邊本來就應該用同一組數字。"""
    wn._verify_generate_one(object(), gen.out)
    assert gen.download_args[0][2].get("max_retries") == 9


def test_the_generation_path_never_reads_a_queue_file():
    """靜態釘：這一支不得出現任何佇列／檢查點常數。

    行為測試擋不住「未來有人在這裡多讀一個檔」——而那個改動在開發機上跑起來完全
    正常，只有在正式批次同時在跑的時候才會互相干擾。
    """
    src = (PKG_ROOT / "webrunner_novelai.py").read_text(encoding="utf-8")
    body = ast.unparse(_func_named(src, "_verify_generate_one"))
    for banned in ("todo_", "TODO_", "CHECKPOINT", "OUTPUT_ROOT", "pop_"):
        assert banned not in body, f"隔離產圖碰到了正式狀態：{banned}"


# ---------------------------------------------------------------------------
# 八、靜態釘：這條路不得碰正式狀態
# ---------------------------------------------------------------------------

_FORBIDDEN_IN_VERIFY = {
    "_sync_chrome_profile_back": "同步回正式登入 profile",
    "_kill_orphan_chrome": "全機 chrome.exe 掃光",
    "claim_liveness_signal": "宣告 webrunner.pid",
    "run_with_liveness_signal": "宣告 webrunner.pid",
}


def _called_names(func: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _func_named(source: str, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"找不到 {name}")


def test_the_verification_path_calls_nothing_that_touches_production_state():
    """行為測試只擋得住**今天**寫在那裡的東西。

    這四個名字任何一個被加進這條路，代價都是一次驗證弄壞一個正在跑的正式批次，
    或者把正式登入態改掉——而且在開發機上「剛好沒跑批次」的那一次會完全正常。
    """
    src = (PKG_ROOT / "webrunner_novelai.py").read_text(encoding="utf-8")
    called = _called_names(_func_named(src, "_run_setup_verification"))
    hits = sorted(called & set(_FORBIDDEN_IN_VERIFY))
    assert not hits, "隔離驗證呼叫了會碰正式狀態的東西：" + "、".join(
        f"{n}（{_FORBIDDEN_IN_VERIFY[n]}）" for n in hits)


def test_the_forbidden_call_check_is_live():
    """先證明抽取器真的看得到呼叫——抓不到任何東西的掃描讀起來跟乾淨一模一樣。"""
    synthetic = (
        "def _run_setup_verification():\n"
        "    _sync_chrome_profile_back()\n"
        "    ws.claim_liveness_signal()\n")
    called = _called_names(_func_named(synthetic, "_run_setup_verification"))
    assert called & set(_FORBIDDEN_IN_VERIFY) == {
        "_sync_chrome_profile_back", "claim_liveness_signal"}
