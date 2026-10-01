"""唯讀的狀態回覆。

`/gen current` 讀的是另一個行程寫的事件檔，壞掉的 `target`（`NaN`、`Infinity`、超大整數）不能
讓它丟例外；`/sys version` 只回短 SHA 與分支名，兩次 git 查詢在執行緒上跑。事件檔與產出根目錄
換到 `tmp_path`，git 換成替身——一個正式檔、一個真的子行程都不碰。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import types

import pytest

import discord_bot as b


def _run(coro, timeout: float = 10.0):
    """跑一段 async 本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _replies(monkeypatch) -> list:
    """把 `safe_reply` 換成記錄器，回 `[content, …]`。"""
    sent: list = []

    async def _reply(_message, content=None, **_kwargs):
        sent.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    return sent



class _Channel:
    """`message.channel`：記下每一則 `send` 的內容。"""

    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, content=None, **_kwargs):
        self.sent.append(content)


def _message(uid: int = 12345) -> types.SimpleNamespace:
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid), channel=_Channel())


def _where_called() -> str:
    """呼叫當下的執行緒上有沒有正在跑的事件迴圈：有＝`"loop"`，否則 `"thread"`。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "thread"
    return "loop"



# ---------------------------------------------------------------------------
# /gen current
# ---------------------------------------------------------------------------
@pytest.fixture
def current_env(monkeypatch, tmp_path):
    """事件檔與產出根目錄換到 `tmp_path`；`alive` 決定背景程式算不算在跑。"""
    events = tmp_path / "events.ndjson"
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(b, "EVENTS_FILE", events)
    monkeypatch.setattr(b, "OUTPUT_ROOT", output)
    state = types.SimpleNamespace(alive=True, events=events, output=output)
    monkeypatch.setattr(b, "_webrunner_alive", lambda: state.alive)

    def write(*rows):
        events.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    state.write = write
    return state


def test_current_without_a_target_shows_no_bar(current_env, monkeypatch):
    current_env.write({"type": "character_start", "name": "carol", "target": None})
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent[-1] == "▶️ currently on **carol**\n `0/0`", sent


@pytest.mark.parametrize("raw_target", ["NaN", "Infinity", "-Infinity", "1" + "0" * 400, "true",
                                        '"abc"', "[3]"])
def test_current_survives_a_broken_target_from_the_events_file(current_env, monkeypatch,
                                                               raw_target):
    """事件檔是另一個行程寫的 JSON，`json.loads` 預設就吃 `NaN`／`Infinity`，整數也沒有上限。
    2026-10-01 之前 `target` 是 NaN 時畫進度條的 `int()` 丟 ValueError，整個 `/gen current`
    只剩內部錯誤；超大整數則讓 `_event_number` 自己丟 OverflowError。壞掉的目標當作 0（不畫進度條）。"""
    current_env.alive = False
    current_env.events.write_text(
        '{"type": "character_start", "name": "a", "target": ' + raw_target + '}\n',
        encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent == ["▶️ currently on **a**\n `0/0`  ⚠️ 背景產圖程式未在執行（可能已停止？）"], sent


def test_event_numbers_too_large_for_a_float_read_as_unknown():
    """`_event_number` 的合約是不丟例外；`float(10**400)` 丟的是 OverflowError，不是 ValueError。"""
    huge = 10 ** 400
    assert b._event_number(huge) is None
    assert b._format_duration_short(huge) == "?"
    assert b._format_count(huge) == "?"
    assert b._event_number(120) == 120.0


# ---------------------------------------------------------------------------
# /sys version
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_git(monkeypatch):
    """`subprocess` 換成只在 bot 模組裡生效的替身：一個真的 git 子行程都不起。"""
    state = types.SimpleNamespace(calls=[], answers={}, where=[], which="C:/git/git.exe")

    def check_output(argv, **kwargs):
        state.calls.append((argv, kwargs))
        state.where.append(_where_called())
        answer = state.answers.get(argv[-1] if argv[1] == "rev-parse" else None)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(b, "subprocess", types.SimpleNamespace(
        check_output=check_output, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr(b._shutil, "which", lambda name: state.which if name == "git" else None)
    return state


def test_version_reports_the_short_sha_and_branch_only(fake_git, monkeypatch):
    """只問短 SHA 與分支名——commit 主旨可能夾帶品牌或路徑，不得外送。"""
    def answer(argv, **kwargs):
        fake_git.calls.append((argv, kwargs))
        return "abc1234\n" if "--short" in argv else "dev\n"

    monkeypatch.setattr(b.subprocess, "check_output", answer)
    sent = _replies(monkeypatch)
    _run(b.mcmd_version(_message()))
    assert sent == ["version: `abc1234` on `dev`"], sent
    argvs = [argv for argv, _kw in fake_git.calls]
    assert argvs == [["git", "rev-parse", "--short", "HEAD"],
                     ["git", "rev-parse", "--abbrev-ref", "HEAD"]]
    for _argv, kwargs in fake_git.calls:
        assert kwargs["cwd"] == str(b.PROJECT_ROOT)
        assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
        assert kwargs["timeout"] == 5 and kwargs["stderr"] is subprocess.DEVNULL


def test_version_without_git_on_path_does_not_run_anything(fake_git, monkeypatch):
    fake_git.which = None
    sent = _replies(monkeypatch)
    _run(b.mcmd_version(_message()))
    assert sent == ["`git` not on PATH; cannot read repo version"], sent
    assert fake_git.calls == []


def test_version_when_the_sha_query_fails_says_so_generically(fake_git, monkeypatch):
    """git 的原始錯誤（可能帶路徑）不進回覆。"""
    fake_git.answers = {"HEAD": subprocess.CalledProcessError(128, ["git"], "fatal: D:/x")}
    sent = _replies(monkeypatch)
    _run(b.mcmd_version(_message()))
    assert sent == ["git query failed"], sent


def test_version_with_an_unknown_branch_still_reports_the_sha(fake_git, monkeypatch):
    def answer(argv, **kwargs):
        fake_git.calls.append((argv, kwargs))
        if "--short" in argv:
            return "abc1234"
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(b.subprocess, "check_output", answer)
    sent = _replies(monkeypatch)
    _run(b.mcmd_version(_message()))
    assert sent == ["version: `abc1234` on `?`"], sent


def test_version_runs_git_off_the_event_loop(fake_git, monkeypatch):
    """兩次 git 查詢各自最多 5 秒，必須丟執行緒（2026-10-01 修）。原本在事件迴圈上同步跑，
    呼叫藏在巢狀的 `_run` 裡，只看 async 本體的靜態掃描看不到。"""
    fake_git.answers = {"HEAD": "abc1234"}
    _replies(monkeypatch)
    _run(b.mcmd_version(_message()))
    assert fake_git.where and set(fake_git.where) == {"thread"}, fake_git.where


