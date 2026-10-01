"""Selenium Manager 暖機：chromedriver 的解析紀錄**到期才**用 `--ttl 86400` 跑一次。

兩個變體開 Chrome 時都把 driver 解析交給 Selenium Manager（je 那一側也是），
而它記在 `se-metadata.json` 的期限預設只有一小時；
過期後的那次解析要重抓約 5 MB 的版本清單，實測每次 Chrome 重啟多花 1～3 分鐘。能把期限
拉長的只有命令列 `--ttl`，而繫結從不傳它，所以開機前由我們自己暖機一次。

這一檔釘住的是四件事，每一件都有一個「看起來比較省事」的錯法：

| 性質 | 錯法 | 代價 |
|---|---|---|
| 只在到期（或讀不懂）時跑 | 每次重啟都跑 | 等於沒省——每次都連網 |
| 跑的是 `--browser chrome --ttl 86400` | 少了 `--ttl` | 暖機照跑，期限仍是一小時，下次照樣連網 |
| 只讀、絕不自己寫 `se-metadata.json` | 省掉 subprocess，自己把數字寫進去 | 寫出去是浮點數，selenium-manager 把整份當成壞的，從此每次都連網（實測 0.030 秒 vs 1.63 秒） |
| 任何失敗都印一行然後繼續 | 讓逾時／找不到執行檔往上丟 | 一個旁路把整批的 Chrome 開機擋掉 |

**這裡的測試絕不能執行真的 selenium-manager，也絕不能讀寫這台機器的 Selenium Manager
快取**：正式批次的 Chrome 每次重啟都靠那份快取。autouse 夾具把 `SE_CACHE_PATH` 指到
暫存目錄，並把 `_webrunner_shared` 看到的 `subprocess` 換成替身——沒有設定腳本的測試一旦
走到執行那一步就當場紅（替身丟 `AssertionError`，暖機只接 `OSError`／`ValueError`／
逾時，所以它不會被吞掉）。
"""
from __future__ import annotations

import ast
import json
import subprocess  # nosec B404
import sys
import types
from pathlib import Path

import pytest
import selenium.webdriver.chrome.service as chrome_service

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import _webrunner_shared as ws  # noqa: E402
import webrunner_je_only as je  # noqa: E402
import webrunner_novelai as wn  # noqa: E402

_VARIANTS = {"webrunner_novelai.py": "build_stealth_driver",
             "webrunner_je_only.py": "start_driver"}
_NOW = 1_790_000_000          # 固定的「現在」（epoch 秒），與正式檔案同一個量級
_MARGIN = ws._SELENIUM_MANAGER_WARMUP_MARGIN_SEC
_FAKE_BINARY = Path("C:/fake/selenium-manager.exe")


# ---------------------------------------------------------------------------
# 夾具與替身
# ---------------------------------------------------------------------------

class _Runner:
    """`_webrunner_shared.subprocess` 的替身：記下每一次 `run`，照腳本回應。

    其他屬性（`TimeoutExpired`、`DEVNULL`、`CREATE_NO_WINDOW`…）轉給真的模組，所以
    暖機的 `except subprocess.TimeoutExpired` 抓得到替身丟的真例外。**只換這個名字，
    不動 stdlib 的 `subprocess.run`**——換後者等於改掉整個行程的行為。
    """

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.outcome = None

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.outcome is None:
            raise AssertionError(
                "這支測試沒有預期 selenium-manager 會被執行——真的 run 在這裡會連網、"
                "改寫 Selenium Manager 的快取")
        return self.outcome(argv, kwargs)

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _Clock:
    """`_webrunner_shared.time` 的替身：牆鐘固定在 `_NOW`，單調時鐘每讀一次走 1.5 秒。"""

    def __init__(self):
        self.now = float(_NOW)
        self.mono = 0.0

    def time(self):
        return self.now

    def monotonic(self):
        self.mono += 1.5
        return self.mono


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    cache = tmp_path / "se-cache"
    cache.mkdir()
    monkeypatch.setenv("SE_CACHE_PATH", str(cache))
    runner = _Runner()
    clock = _Clock()
    monkeypatch.setattr(ws, "subprocess", runner)
    monkeypatch.setattr(ws, "time", clock)
    ns = types.SimpleNamespace(cache=cache, meta=cache / "se-metadata.json",
                               runner=runner, clock=clock, located=[])
    # 前提檢查：下面每一支都以為自己讀的是暫存目錄。這一句不成立的話，測試會去讀
    # （甚至讓暖機去改寫）這台機器真的快取。
    assert ws.selenium_manager_cache_dir() == cache
    return ns


def _driver(ttl, major="153", version="153.0.8010.52", name="chromedriver"):
    return {"major_browser_version": major, "driver_name": name,
            "driver_version": version, "driver_ttl": ttl}


def _write_metadata(path: Path, drivers) -> None:
    """照正式 `se-metadata.json` 的形狀（2026-09-26 唯讀看過）寫一份。"""
    path.write_text(json.dumps({
        "browsers": [],
        "drivers": drivers,
        "stats": [{"browser": "chrome", "browser_version": "", "os": "windows",
                   "arch": "x86_64", "lang": "python", "selenium_version": "4.48",
                   "stats_ttl": _NOW + 3545}],
        "cached_assets": [{"asset_name": "chromedriver",
                           "asset_version": "153.0.8010.52",
                           "last_used": _NOW - 60}],
    }), encoding="utf-8")


def _locate(env):
    def locate():
        env.located.append(True)
        return _FAKE_BINARY
    return locate


def _extends_ttl(env, seconds=86399, rc=0):
    """selenium-manager 成功的樣子：它自己把 metadata 寫成新的整數期限。"""
    def outcome(argv, _kwargs):
        _write_metadata(env.meta, [_driver(int(env.clock.now) + seconds)])
        stdout = json.dumps({"logs": [], "result": {
            "code": rc, "message": "", "driver_path": "C:/x/chromedriver.exe"}})
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")
    return outcome


# ---------------------------------------------------------------------------
# 一、讀 metadata：什麼時候信得過
# ---------------------------------------------------------------------------

def test_the_real_file_shape_is_read_as_an_int_expiry(env):
    """正式檔案的形狀：`drivers` 一筆 chromedriver、`driver_ttl` 是整數。"""
    _write_metadata(env.meta, [_driver(_NOW + 100)])
    ttl, detail = ws.read_chromedriver_ttl(env.meta)
    assert ttl == _NOW + 100 and type(ttl) is int
    assert "153.0.8010.52" in detail and "153" in detail


def test_the_newest_chrome_major_wins_and_other_drivers_are_ignored(env):
    """Chrome 換主版號時舊的那筆會留到下次寫檔；看的是主版號最大的那一筆。

    主版號要**當數字比**：字串比的話 `"99" > "153"`。同主版號重複時取期限最晚的。
    """
    _write_metadata(env.meta, [
        _driver(_NOW + 9_000, major="99", version="99.0.1"),
        _driver(_NOW - 10, major="153"),
        _driver(_NOW + 50, major="153"),
        _driver(_NOW + 99_999, major="200", name="geckodriver"),
    ])
    ttl, detail = ws.read_chromedriver_ttl(env.meta)
    assert ttl == _NOW + 50, detail


@pytest.mark.parametrize("label, bad_ttl", [
    ("float", _NOW + 86400.5),
    ("integral float", float(_NOW + 86400)),
    ("bool", True),
    ("string", str(_NOW + 86400)),
    ("missing", None),
])
def test_a_non_integer_expiry_anywhere_makes_the_whole_file_untrusted(
        env, label, bad_ttl):
    """selenium-manager 反序列化失敗時丟掉的是**整份**檔案，所以一筆壞的就等於沒有紀錄。

    浮點數正是「自己把數字寫進去」會寫出來的東西（`json.dump(time.time() + 86400)`），
    而 `bool` 是 `int` 的子類別，要另外排除。壞的那一筆刻意放在**另一個** driver 上：
    只檢查 chromedriver 那一筆的話，它會被讀成一份好好的、還沒到期的紀錄。
    """
    other = _driver(bad_ttl, name="geckodriver")
    if bad_ttl is None:
        del other["driver_ttl"]
    _write_metadata(env.meta, [_driver(_NOW + 86400), other])
    ttl, detail = ws.read_chromedriver_ttl(env.meta)
    assert ttl is None, f"{label}: {detail}"
    assert "driver_ttl" in detail


@pytest.mark.parametrize("label, content", [
    ("not json", b"{not json"),
    ("not utf-8", b"\xff\xfe{}"),
    ("top level list", b"[]"),
    ("drivers not a list", b'{"drivers": {}}'),
    ("entry not a dict", b'{"drivers": ["x"]}'),
    ("no chromedriver", json.dumps({"drivers": [
        _driver(_NOW + 86400, name="geckodriver")]}).encode("utf-8")),
    ("empty drivers", b'{"drivers": []}'),
])
def test_a_garbled_file_is_untrusted_and_says_why(env, label, content):
    env.meta.write_bytes(content)
    ttl, detail = ws.read_chromedriver_ttl(env.meta)
    assert ttl is None, label
    assert detail, label


def test_a_missing_file_is_untrusted(env):
    ttl, detail = ws.read_chromedriver_ttl(env.meta)
    assert ttl is None and "se-metadata.json" in detail


def test_the_cache_dir_follows_se_cache_path_and_falls_back_to_home(
        env, monkeypatch, tmp_path):
    """selenium-manager 自己認 `SE_CACHE_PATH`；暖機讀的必須是繫結等一下用的同一份。"""
    monkeypatch.setenv("SE_CACHE_PATH", str(tmp_path / "elsewhere"))
    assert ws.selenium_manager_cache_dir() == tmp_path / "elsewhere"
    monkeypatch.setenv("SE_CACHE_PATH", "   ")
    assert ws.selenium_manager_cache_dir() == Path.home() / ".cache" / "selenium"
    monkeypatch.delenv("SE_CACHE_PATH")
    assert ws.selenium_manager_cache_dir() == Path.home() / ".cache" / "selenium"


# ---------------------------------------------------------------------------
# 二、什麼時候跑、跑的是什麼
# ---------------------------------------------------------------------------

def test_an_expired_record_runs_the_warmup_once_with_the_long_ttl(env, capsys):
    """到期 → 跑一次，參數正是量過有效的那一組，輸出用 UTF-8 解、等待有上限。"""
    _write_metadata(env.meta, [_driver(_NOW - 3600)])
    env.runner.outcome = _extends_ttl(env)
    assert ws.warm_up_selenium_manager(_locate(env)) is True

    assert len(env.runner.calls) == 1
    argv, kwargs = env.runner.calls[0]
    assert argv[0] == str(_FAKE_BINARY)
    assert argv[1:] == ["--browser", "chrome", "--ttl", "86400", "--output", "json"]
    assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
    assert kwargs["capture_output"] is True
    assert 0 < kwargs["timeout"] <= 600, kwargs["timeout"]
    assert kwargs.get("shell") is not True
    out = capsys.readouterr().out
    assert "warm-up done" in out and "24.0 h" in out, out


def test_a_fresh_record_does_not_run_or_even_look_for_the_binary(env, capsys):
    """還沒到期 → 什麼都不跑，連 locator 都不叫。每次重啟都跑就等於沒省。"""
    _write_metadata(env.meta, [_driver(_NOW + 5 * 3600)])
    assert ws.warm_up_selenium_manager(_locate(env)) is True
    assert env.runner.calls == [] and env.located == []
    assert "expires in 5.0 h; no warm-up needed" in capsys.readouterr().out


@pytest.mark.parametrize("offset, runs", [
    (_MARGIN + 1, False),     # 剛好還夠用
    (_MARGIN, True),          # 剩下的正好等於餘裕 → 算到期
    (1, True),                # selenium-manager 自己還認為沒到期，但繫結用到時可能就過了
    (0, True),
    (-1, True),
])
def test_the_expiry_boundary_includes_the_safety_margin(env, offset, runs):
    """邊界：剩不到 `_SELENIUM_MANAGER_WARMUP_MARGIN_SEC` 也算到期。

    這裡讀的時刻與繫結真正解析的時刻之間隔著 profile 快照與最多三次 spawn 嘗試；
    沒有餘裕的話，剛好在那段時間裡到期的那一次會讓繫結照樣連網。
    """
    _write_metadata(env.meta, [_driver(_NOW + offset)])
    env.runner.outcome = _extends_ttl(env)
    ws.warm_up_selenium_manager(_locate(env))
    assert (len(env.runner.calls) == 1) is runs, offset


@pytest.mark.parametrize("label, content", [
    ("missing", None),
    ("garbled", b"{not json"),
    ("float ttl", json.dumps({"drivers": [
        _driver(_NOW + 86400.25)]}).encode("utf-8")),
    ("bool ttl", json.dumps({"drivers": [_driver(True)]}).encode("utf-8")),
])
def test_an_untrusted_record_runs_the_warmup(env, label, content):
    """讀不懂就當到期：暖機會讓 selenium-manager 把整份寫回乾淨的整數版。"""
    if content is not None:
        env.meta.write_bytes(content)
    env.runner.outcome = _extends_ttl(env)
    assert ws.warm_up_selenium_manager(_locate(env)) is True, label
    assert len(env.runner.calls) == 1, label


def test_the_warmup_never_writes_the_metadata_itself(env):
    """**只讀不寫**：就算 selenium-manager 什麼都沒改，檔案也要原封不動。

    最順手的捷徑是「省掉那次 subprocess，自己把新期限寫進去」，而那會寫出浮點數、讓
    整份 metadata 安靜失效。這裡餵一份已經壞掉（浮點數）的檔案，讓 selenium-manager
    的替身什麼都不做——我們的程式不得自己去「修」它。
    """
    env.meta.write_bytes(json.dumps({"drivers": [
        _driver(_NOW + 86400.25)]}).encode("utf-8"))
    before = env.meta.read_bytes()
    env.runner.outcome = lambda argv, _kw: subprocess.CompletedProcess(
        argv, 0, stdout='{"logs": [], "result": {"code": 0}}', stderr="")
    assert ws.warm_up_selenium_manager(_locate(env)) is False
    assert env.meta.read_bytes() == before


# ---------------------------------------------------------------------------
# 三、失敗一律印一行、回 False、不往上丟
# ---------------------------------------------------------------------------

def _raise(error):
    def outcome(_argv, _kwargs):
        raise error
    return outcome


@pytest.mark.parametrize("label, outcome, expected", [
    ("timeout", _raise(subprocess.TimeoutExpired(["selenium-manager"], 180)),
     "within 180 s"),
    ("binary vanished", _raise(FileNotFoundError(2, "nope")),
     "could not be started (FileNotFoundError)"),
    ("permission", _raise(PermissionError(13, "denied")),
     "could not be started (PermissionError)"),
    ("network failure", lambda argv, _kw: subprocess.CompletedProcess(
        argv, 65, stdout=json.dumps({"logs": [], "result": {
            "code": 65, "message": "error sending request for url",
            "driver_path": ""}}), stderr=""),
     "exit code 65"),
    ("panic without json", lambda argv, _kw: subprocess.CompletedProcess(
        argv, 101, stdout="",
        stderr="\nthread 'main' panicked at src\\config.rs:247\n\n"),
     "panicked at"),
])
def test_every_failure_is_one_line_and_never_raises(env, capsys, label, outcome,
                                                    expected):
    """暖機是旁路：失敗只會退回今天的行為（交給繫結解析），不可以擋住開機。"""
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = outcome
    assert ws.warm_up_selenium_manager(_locate(env)) is False, label
    err = capsys.readouterr().err
    assert expected in err, (label, err)
    assert "leaving resolution to Selenium Manager as usual" in err, label


def test_the_failure_message_is_taken_from_the_json_result(env, capsys):
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = lambda argv, _kw: subprocess.CompletedProcess(
        argv, 65, stdout=json.dumps({"logs": [
            {"level": "WARN", "timestamp": 1, "message": "older warning"}],
            "result": {"code": 65, "message": "error sending request"}}),
        stderr="")
    ws.warm_up_selenium_manager(_locate(env))
    err = capsys.readouterr().err
    assert "error sending request" in err and "older warning" not in err


def test_a_note_the_console_cannot_encode_does_not_crash_the_failure_line(
        env, capsys):
    """失敗路徑上印外部文字：stderr 接到 cp950 管線時，印不出來的字元會讓 `print`
    本身丟 `UnicodeEncodeError`。訊息必須在任何碼頁上都印得出來。"""
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = lambda argv, _kw: subprocess.CompletedProcess(
        argv, 65, stdout=json.dumps({"logs": [], "result": {
            "code": 65, "message": "failed \u2603 \U0001f600"}}), stderr="")
    ws.warm_up_selenium_manager(_locate(env))
    line = [ln for ln in capsys.readouterr().err.splitlines() if "exit code" in ln][0]
    line.encode("cp950")
    assert "\\u2603" in line


def test_a_version_string_from_the_file_cannot_crash_the_fresh_line(env, capsys):
    """還新鮮的那一行也印了檔案裡的字串（driver 版本）。檔案是外部輸入，所以它一樣要在
    cp950 上印得出來——這一行每次開機都會印，壞掉的話就是每次開機都丟例外。"""
    _write_metadata(env.meta, [_driver(_NOW + 5 * 3600, version="153☃",
                                       major="153")])
    assert ws.warm_up_selenium_manager(_locate(env)) is True
    line = [ln for ln in capsys.readouterr().out.splitlines() if "no warm-up needed" in ln][0]
    line.encode("cp950")
    assert "153\\u2603" in line


def test_a_locator_that_raises_is_reported_and_nothing_is_run(env, capsys):
    """找不到內附執行檔時 selenium 丟 `WebDriverException`、私有 API 改名則是
    `AttributeError`——共用模組叫不出前者的名字，所以這一步必須自己接得住。"""
    _write_metadata(env.meta, [_driver(_NOW - 60)])

    class DriverLibraryError(Exception):
        pass

    def locate():
        raise DriverLibraryError("Unable to obtain working Selenium Manager binary")

    assert ws.warm_up_selenium_manager(locate) is False
    assert env.runner.calls == []
    err = capsys.readouterr().err
    assert ("the selenium-manager executable cannot be found" in err
            and "DriverLibraryError" in err)


def test_a_run_that_left_the_record_expired_is_not_reported_as_success(
        env, capsys):
    """**以結果為準**：回 0 卻沒有把期限寫新，就不能印「暖機完成」。"""
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = lambda argv, _kw: subprocess.CompletedProcess(
        argv, 0, stdout='{"logs": [], "result": {"code": 0}}', stderr="")
    assert ws.warm_up_selenium_manager(_locate(env)) is False
    captured = capsys.readouterr()
    assert "warm-up done" not in captured.out
    assert "still unusable" in captured.err


def test_a_run_that_only_got_the_default_hour_says_so(env, capsys):
    """期限只拉到一小時（某一版不再理會 `--ttl`）：之後每次重啟都會再暖機一次、
    每次白抓一次——長得跟正常一模一樣，所以要明講。"""
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = _extends_ttl(env, seconds=3600)
    assert ws.warm_up_selenium_manager(_locate(env)) is True
    captured = capsys.readouterr()
    assert "warm-up done" not in captured.out
    assert "`--ttl` may not have been honoured" in captured.err


# ---------------------------------------------------------------------------
# 四、接線：兩個變體都在開 Chrome 之前暖機一次，失敗也照樣開得起來
# ---------------------------------------------------------------------------

class _FakeService:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _novelai_boot(monkeypatch, tmp_path, env):
    order: list[str] = []
    real_run = env.runner.run

    def run(argv, **kwargs):
        order.append("warmup-run")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", run)
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", tmp_path / "cd.log")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", tmp_path / "cd.prev.log")
    monkeypatch.setattr(chrome_service, "Service", _FakeService)
    monkeypatch.setattr(wn, "_trim_chromedriver_log", lambda: order.append("trim"))
    monkeypatch.setattr(wn, "_rotate_chromedriver_log",
                        lambda: order.append("rotate"))
    monkeypatch.setattr(wn, "_snapshot_chrome_profile",
                        lambda: (order.append("snapshot"), tmp_path)[1])
    monkeypatch.setattr(wn, "_clear_snapshot_locks", lambda _snap: None)
    monkeypatch.setattr(wn, "_selenium_manager_binary", lambda: _FAKE_BINARY)
    monkeypatch.setattr(ws, "log_driver_versions", lambda _caps: None)
    monkeypatch.setattr(wn.port, "driver", wn.port.driver)

    class FakeChrome:
        def __init__(self, **_kwargs):
            order.append("chrome")
            self.capabilities = {}

        def execute_cdp_cmd(self, *_args):
            return None

    monkeypatch.setattr(wn.webdriver, "Chrome", FakeChrome)
    return order


def _je_boot(monkeypatch, tmp_path, env):
    order: list[str] = []
    real_run = env.runner.run

    def run(argv, **kwargs):
        order.append("warmup-run")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(env.runner, "run", run)
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG", tmp_path / "cd.log")
    monkeypatch.setattr(ws, "_CHROMEDRIVER_LOG_PREV", tmp_path / "cd.prev.log")
    monkeypatch.setattr(chrome_service, "Service", _FakeService)
    monkeypatch.setattr(je, "_trim_chromedriver_log", lambda: order.append("trim"))
    monkeypatch.setattr(je, "_rotate_chromedriver_log",
                        lambda: order.append("rotate"))
    monkeypatch.setattr(je, "_snapshot_chrome_profile",
                        lambda: (order.append("snapshot"), tmp_path)[1])
    monkeypatch.setattr(je, "_clear_snapshot_locks", lambda _snap: None)
    monkeypatch.setattr(je, "_selenium_manager_binary", lambda: _FAKE_BINARY)
    monkeypatch.setattr(ws, "log_driver_versions", lambda _caps: None)
    monkeypatch.setattr(je, "_CURRENT_SNAPSHOT_PROFILE", None)
    monkeypatch.setattr(je, "wr", types.SimpleNamespace(
        set_driver=lambda *_a, **_k: order.append("chrome"),
        current_webdriver=types.SimpleNamespace(capabilities={}),
        add_script_to_evaluate_on_new_document=lambda _script: None))
    return order


_BOOTS = {
    "novelai": (_novelai_boot, lambda: wn.build_stealth_driver()),
    "je": (_je_boot, lambda: je.start_driver()),
}


@pytest.mark.parametrize("variant", sorted(_BOOTS))
@pytest.mark.parametrize("label, outcome", [
    ("timeout", _raise(subprocess.TimeoutExpired(["selenium-manager"], 180))),
    ("binary vanished", _raise(FileNotFoundError(2, "nope"))),
    ("network failure", lambda argv, _kw: subprocess.CompletedProcess(
        argv, 65, stdout="", stderr="error sending request")),
])
def test_a_failed_warmup_still_boots_chrome(env, monkeypatch, tmp_path, variant,
                                            label, outcome):
    """真的暖機（替身的 selenium-manager 失敗）接在真的開機路徑上：Chrome 照樣開。"""
    boot, start = _BOOTS[variant]
    order = boot(monkeypatch, tmp_path, env)
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = outcome
    start()
    assert order.count("chrome") == 1, (label, order)
    assert order.index("warmup-run") < order.index("chrome"), order


@pytest.mark.parametrize("variant", sorted(_BOOTS))
def test_the_boot_warms_up_first_and_only_when_expired(env, monkeypatch,
                                                       tmp_path, variant):
    """到期：暖機排在快照之前（`snapshot profile` → `[driver]` 的間隔才只量到繫結
    解析本身）；緊接著再開一次機，期限已經是新的——這次不跑。"""
    boot, start = _BOOTS[variant]
    order = boot(monkeypatch, tmp_path, env)
    _write_metadata(env.meta, [_driver(_NOW - 60)])
    env.runner.outcome = _extends_ttl(env)
    start()
    assert order[0] == "warmup-run", order
    assert order.index("warmup-run") < order.index("snapshot") < order.index("chrome")
    order.clear()
    start()
    assert "warmup-run" not in order and order.count("chrome") == 1, order


def _spawn_function(filename: str) -> ast.FunctionDef:
    tree = ast.parse((PKG_ROOT / filename).read_text(encoding="utf-8"), filename)
    return next(node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == _VARIANTS[filename])


def _warmup_statements(func: ast.FunctionDef) -> list[tuple[int, bool]]:
    """`(行號, 是不是函式本體的頂層陳述)`，每一個 `ws.warm_up_selenium_manager(...)` 呼叫。"""
    top = {id(stmt) for stmt in func.body}
    out = []
    for node in ast.walk(func):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == "ws.warm_up_selenium_manager"):
            args = [ast.unparse(a) for a in node.value.args]
            assert args == ["_selenium_manager_binary"], args
            out.append((node.lineno, id(node) in top))
    return sorted(out)


@pytest.mark.parametrize("filename", sorted(_VARIANTS))
def test_both_spawn_paths_warm_up_once_before_anything_else(filename):
    """兩個變體都要有，而且是函式本體的**頂層**陳述、排在記錄檔封頂之前。

    頂層＝不在重試迴圈、也不在任何 `try` 裡：放進迴圈會每次嘗試都讀一次（到期時甚至
    每次都跑），包進 spawn 的 `try` 則會讓它的失敗被算成「Chrome 起不來」。

    je 那一側也要有：`je_web_runner` 的 `install()` 回傳值被丟掉，driver 一樣由
    Selenium Manager 解析（§8.40），到期時一樣要多等那 1～3 分鐘。
    """
    func = _spawn_function(filename)
    calls = _warmup_statements(func)
    assert len(calls) == 1, f"{filename}：暖機呼叫有 {len(calls)} 個，預期剛好 1 個"
    line, at_top = calls[0]
    assert at_top, f"{filename}:{line} 暖機不在函式本體的頂層（迴圈或 try 裡？）"
    trims = [node.lineno for node in ast.walk(func)
             if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "_trim_chromedriver_log"]
    assert trims and line < min(trims), (filename, line, trims)


def test_the_statement_scanner_sees_a_call_inside_a_loop():
    """正面對照：掃描器要分得出「頂層」與「迴圈裡」，否則上一支永遠綠。"""
    func = ast.parse(
        "def build_stealth_driver():\n"
        "    for _ in range(3):\n"
        "        ws.warm_up_selenium_manager(_selenium_manager_binary)\n"
        "    ws.warm_up_selenium_manager(_selenium_manager_binary)\n").body[0]
    assert _warmup_statements(func) == [(3, False), (4, True)]


# ---------------------------------------------------------------------------
# 五、selenium 那一側的介面：locator 用的是私有 API，所以要有人盯著
# ---------------------------------------------------------------------------

def test_the_installed_selenium_still_locates_its_bundled_manager(monkeypatch):
    """`SeleniumManager._get_binary` 是私有 API（selenium 沒有公開的替代）。

    它改名或換簽名的話，暖機只會印一行然後略過——不會壞掉任何東西，但從此每次重啟又
    回到連網，而且沒有人會發現。所以在這裡對**裝著的** selenium 先紅。兩個變體的
    locator 要回同一個、確實存在的檔。
    """
    monkeypatch.delenv("SE_MANAGER_PATH", raising=False)
    from selenium.webdriver.common.selenium_manager import SeleniumManager
    assert callable(getattr(SeleniumManager, "_get_binary", None))
    path = wn._selenium_manager_binary()
    assert Path(path).is_file(), path
    assert Path(path).stem == "selenium-manager", path
    assert je._selenium_manager_binary() == path
