"""`run_batch.py` 的守門測試。

這支是 repo root 的批次進入點（PyCharm 一鍵啟動），在 2026-09-09 之前**一支
測試都沒有**，而它會刪檔案、也會 spawn 子行程。

兩件事釘在這裡：

1. **中止的路徑不可以留下持久的副作用。** `--clear-pause` 原本在確認「有沒有
   別的 instance 在跑」**之前**就把 `webrunner.pause` 刪掉了，於是「正式批次
   正在跑而且被暫停中」的情況下，這支會一邊把那個批次放跑、一邊回 rc=1 說
   「先打 !stop」。使用者看到的是「什麼都沒做」。
2. **它不可以自己 spawn webrunner。** `_bot_spawned_pid()` 讀不出 pid 時回
   `None`（＝樂觀往下跑），那個 docstring 明寫「在這支是安全的，因為它不
   spawn webrinner，只把工作交給 `start_webrunner.py`，而那支會在持有 Chrome
   槽的狀態下再判一次」。**那個前提沒有任何東西在守。**

**安全**：每一支會呼叫 `main()` 的測試都先把 `WEBRUNNER_PAUSE_FILE` 與
`PID_FILE` 導到 `tmp_path`，並且斷言導成功了才往下跑——這台機器上有長時間執行
的正式批次，誤刪那兩個檔案的代價是真的。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run_batch as rb  # noqa: E402

RUN_BATCH_SOURCE = (REPO_ROOT / "run_batch.py").read_text(encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把所有會被寫到的路徑導進 `tmp_path`，並確認真的導開了。

    這個 fixture 本身就是一道保險：斷言在**測試主體之前**，所以哪天有人把常數
    改名、`monkeypatch.setattr` 打空，測試會停在這裡，而不是拿著正式路徑往下跑。
    """
    pause = tmp_path / "webrunner.pause"
    pid_file = tmp_path / "webrunner.pid"
    monkeypatch.setattr(rb, "WEBRUNNER_PAUSE_FILE", pause)
    monkeypatch.setattr(rb, "PID_FILE", pid_file)
    assert rb.WEBRUNNER_PAUSE_FILE.parent == tmp_path
    assert rb.PID_FILE.parent == tmp_path
    # 佇列快照是唯讀的，但沒必要在單元測試裡去讀正式佇列。
    monkeypatch.setattr(rb, "run_preflight", lambda **kwargs: False)
    return tmp_path


def _run(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["run_batch.py", *argv])
    return rb.main()


# ---------------------------------------------------------------------------
# 1) 中止的路徑不可以留下持久的副作用
# ---------------------------------------------------------------------------
def test_clear_pause_keeps_the_marker_when_a_batch_is_already_running(
        sandbox, monkeypatch, capsys):
    """最重要的一支：正式批次在跑、而且被暫停中，`--clear-pause` 不可以放跑它。

    修正之前，第 1 步就 `unlink()` 了，第 2 步才中止——刪除是留在磁碟上的，
    而正在等在安全邊界的那個批次會立刻恢復產圖。使用者只看到「先打 !stop」。
    """
    rb.WEBRUNNER_PAUSE_FILE.write_text("", encoding="utf-8")
    rb.PID_FILE.write_text("4321", encoding="utf-8")
    monkeypatch.setattr(rb, "_pid_alive", lambda pid: True)

    rc = _run(monkeypatch, "--clear-pause")

    assert rc == 1, "有 instance 在跑時應該中止"
    assert rb.WEBRUNNER_PAUSE_FILE.exists(), (
        "中止了卻把 pause 標記刪掉了——正在暫停中的正式批次會被放跑，"
        "而使用者看到的訊息說的是「什麼都沒做」")
    assert "已清除" not in capsys.readouterr().out


def test_clear_pause_still_clears_the_marker_when_nothing_is_running(
        sandbox, monkeypatch, capsys):
    """正面對照組：把 `unlink()` 整句刪掉，上面那支也會通過。

    `--clear-pause` 在安全的情況下**必須真的清掉標記**，否則這個旗標等於失效，
    而失效的症狀是「殘留標記還在、下次開跑又停住」——看起來像別的問題。
    """
    rb.WEBRUNNER_PAUSE_FILE.write_text("", encoding="utf-8")
    monkeypatch.setattr(rb, "_pid_alive", lambda pid: False)

    _run(monkeypatch, "--clear-pause")

    assert not rb.WEBRUNNER_PAUSE_FILE.exists(), "沒有清掉殘留的 pause 標記"
    assert "已清除" in capsys.readouterr().out


def test_a_stale_marker_without_the_flag_aborts_and_keeps_it(
        sandbox, monkeypatch):
    """沒帶旗標時只中止、不刪——刪除必須是使用者明確要求的。"""
    rb.WEBRUNNER_PAUSE_FILE.write_text("", encoding="utf-8")
    monkeypatch.setattr(rb, "_pid_alive", lambda pid: False)

    rc = _run(monkeypatch)

    assert rc == 1
    assert rb.WEBRUNNER_PAUSE_FILE.exists(), "沒帶 --clear-pause 卻刪掉了標記"


def test_a_live_batch_aborts_before_reading_the_queues(sandbox, monkeypatch):
    """有 instance 在跑就該立刻中止，不必再去讀四個佇列檔。

    這一支同時把「順序」釘住：`run_preflight` 被呼叫到，就代表 instance 檢查
    跑在它後面了。
    """
    calls = []
    monkeypatch.setattr(rb, "run_preflight",
                        lambda **kwargs: calls.append(kwargs) or False)
    rb.PID_FILE.write_text("4321", encoding="utf-8")
    monkeypatch.setattr(rb, "_pid_alive", lambda pid: True)

    assert _run(monkeypatch) == 1
    assert not calls, "已經確定有 instance 在跑，卻還去讀了佇列"


@pytest.mark.parametrize("raw", ["", "   ", "not-a-pid", "\udcff"])
def test_an_unreadable_pid_file_does_not_crash(sandbox, monkeypatch, raw):
    """讀不出 pid 時走樂觀那一邊（往下跑），但**不可以炸**。

    `UnicodeDecodeError` 是 `ValueError` 的子類，所以那個 `except` 連帶接住了
    它——啟動器 2026-09-07 修掉的正是「沒接住、直接炸穿」。
    """
    rb.PID_FILE.write_bytes(raw.encode("utf-8", "surrogateescape"))
    assert _run(monkeypatch) == 1        # 被 run_preflight 的 False 擋下來


# ---------------------------------------------------------------------------
# 2) 它不可以自己 spawn webrunner（`_bot_spawned_pid` 的安全性論證的前提）
# ---------------------------------------------------------------------------
_WEBRUNNER_MODULE = re.compile(r"webrunner_[a-z_]+\.py")


def _direct_webrunner_spawns(source: str) -> list[str]:
    """原始碼裡有沒有直接指名某個 webrunner 變體的字串常數。

    抽成純函式是因為現況是乾淨的——下面那支真實資料的測試就算把斷言刪掉也全綠。
    真正的牙齒在 `test_the_spawn_scanner_would_see_a_direct_call`。
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(_WEBRUNNER_MODULE.findall(node.value))
    return sorted(set(found))


def test_run_batch_never_names_a_webrunner_variant_directly():
    """`_bot_spawned_pid()` 讀不出 pid 時回 `None`（＝往下跑）**只在這支不自己
    spawn webrunner 時才安全**——它的 docstring 明寫了這個前提，而且明寫了
    「如果哪天改成自己直接 spawn，這個 `None` 就會變成真正的缺陷」。

    那個前提在 2026-09-09 之前沒有任何東西在守。失效沒有症狀：兩個 instance
    同時搶 `.chrome_profile/`，而搶輸的那個不會說話。
    """
    # 正面對照組：來源讀空的話下面那句會空轉通過。
    assert len(RUN_BATCH_SOURCE) > 500, "run_batch.py 讀起來太短，讀取壞了"
    assert "start_webrunner.py" in RUN_BATCH_SOURCE, (
        "run_batch.py 沒有提到 `start_webrunner.py`——交棒的對象換掉了的話，"
        "`_bot_spawned_pid` 那段安全性論證要重寫")

    offenders = _direct_webrunner_spawns(RUN_BATCH_SOURCE)
    assert not offenders, (
        f"run_batch.py 直接指名了 {offenders}。這支必須把 spawn 交給 "
        "`start_webrunner.py`——那支才會在持有 Chrome 槽的狀態下再判一次"
        "「是不是已經有 instance 在跑」。自己 spawn 的話，`_bot_spawned_pid()` "
        "讀不出 pid 時的樂觀回傳會變成兩個批次搶同一個 Chrome profile。")


def test_the_spawn_scanner_would_see_a_direct_call():
    """合成資料的對照組：沒有這支，上面那個掃描器直接 `return []` 也全綠。"""
    assert _direct_webrunner_spawns(
        'x = ["py", "webrunner_novelai.py"]') == ["webrunner_novelai.py"]
    assert _direct_webrunner_spawns('x = "start_webrunner.py"') == []


def test_there_is_exactly_one_place_that_spawns_a_child():
    """交棒點只能有一個。

    多一個 spawn 點就多一條繞過 `start_webrunner.py` 那道判定的路，而這種
    「多開了一份」的錯不會當場報錯——它會表現成產圖結果互相覆蓋。
    """
    spawns = [
        node for node in ast.walk(ast.parse(RUN_BATCH_SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert len(spawns) == 1, (
        f"`subprocess.*` 的呼叫點有 {len(spawns)} 個，預期只有一個"
        "（交給 `start_webrunner.py` 的那一個）")


def test_the_source_length_floor_fires_when_the_read_comes_back_empty(
        monkeypatch):
    """那道下限自己的對照組。

    `RUN_BATCH_SOURCE` 讀空的時候，「沒有直接指名變體」與「根本沒讀到東西」在輸出
    上一模一樣——把 `> 500` 放寬成 `> 0`，整支照樣綠。所以要把前提直接打壞。

    **必須斷言是哪一句在叫**：語料換空之後，下一句
    `assert "start_webrunner.py" in RUN_BATCH_SOURCE` 也會炸出 `AssertionError`，
    只寫 `pytest.raises(AssertionError)` 的控制組會被它餵飽而放走真正的變異
    （§8.8(A4) 的那個變體，本專案已經踩過三次）。
    """
    # **不能餵空字串**：`> 500` 與放寬過的 `> 0` 對長度 0 是同一個答案，所以那個
    # 變異會存活（實測溜掉過一次）。餵一段短、但非空、而且**仍然提到
    # `start_webrunner.py`** 的來源——後者是為了讓下一句斷言不要先炸掉，否則控制
    # 測試會拿到一個訊息不對的 `AssertionError` 然後綠著放走變異。
    monkeypatch.setattr(sys.modules[__name__], "RUN_BATCH_SOURCE",
                        "# 交棒給 start_webrunner.py\n" * 3)
    with pytest.raises(AssertionError) as excinfo:
        test_run_batch_never_names_a_webrunner_variant_directly()
    assert "讀取壞了" in str(excinfo.value), (
        f"紅的不是長度下限那一句，而是：{excinfo.value}")
