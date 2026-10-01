"""唯讀的狀態回覆。

`/gen current`、`/out history`、`/preset show`、`/sys churn`、`/sys dashboard`、`/help`、
`/sys introspect_dom`、`/sys version`。它們不改使用者資料，但都是使用者拿來**判斷現況**的
入口：講錯一句（把「角色之間」講成「沒在跑」、把休息講成卡住、把舊圖排在新圖前面）比不講
還糟，因為會被拿去做決定。

全部換到 `tmp_path`：事件檔、產出根目錄、提示詞檔、請求檔；git 與工作區掃描換成替身——
這台機器上跑著正式的批次與 bot，一個正式檔、一個真的子行程都不碰。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
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


def test_current_with_no_run_says_nothing_is_running(current_env, monkeypatch):
    current_env.alive = False
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent == ["目前沒有進行中的角色 — 背景產圖程式未在執行"], sent


def test_current_between_characters_is_not_reported_as_stopped(current_env, monkeypatch):
    """上一個角色收工了、下一個還沒開始：程式在跑，只是不在任何角色上。"""
    now = time.time()
    current_env.write(
        {"type": "character_start", "name": "alice", "target": 10, "ts": now - 50},
        {"type": "character_done", "name": "alice", "ts": now - 10})
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent == ["背景產圖程式**執行中**，但尚未開始任何角色（仍在初始化 / 角色之間）"], sent


def test_current_during_a_scheduled_rest_says_when_it_wakes(current_env, monkeypatch):
    """排程休息可以長達好幾小時；只說「角色之間」會把計畫中的閒置講得像出了事。"""
    wake = time.time() + 3 * 3600 + 30
    current_env.write({"type": "schedule_rest", "wake_ts": wake})
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    clock = time.strftime("%m-%d %H:%M", time.localtime(wake))
    assert sent[-1].startswith(f"💤 背景產圖程式**執行中**，正在排程休息，預計 `{clock}` 繼續"), sent
    assert re.search(r"還有 `3h (29|30)s`", sent[-1]), sent


def test_current_shows_progress_elapsed_and_eta(current_env, monkeypatch):
    """張數是**產出資料夾裡實際的檔案**（不是事件裡的數字），只算直屬的圖檔：文字檔、子資料夾
    裡的圖都不算。剩餘時間＝還差幾張 × 每張秒數。"""
    folder = current_env.output / "alice_folder"
    folder.mkdir()
    for name in ("a.png", "b.JPG", "c.webp", "d.jpeg", "notes.txt"):
        (folder / name).write_bytes(b"x")
    (folder / "sub").mkdir()
    (folder / "sub" / "e.png").write_bytes(b"x")
    current_env.write({"type": "character_start", "name": "alice", "target": 10,
                       "folder": "alice_folder", "ts": time.time() - 125.4})
    monkeypatch.setattr(b, "_seconds_per_image",
                        lambda events=None, **_kw: (30.0, True, "window", True))
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    first, second, third = sent[-1].split("\n")
    assert re.fullmatch(r"▶️ currently on \*\*alice\*\*, started 2m (5|6)s ago", first), first
    assert second == "[" + "█" * 8 + "░" * 12 + "] `4/10` 40%", second
    assert third == "~`3m 0s` left for this character", third


def test_current_warns_when_the_process_is_gone_and_drops_the_eta(current_env, monkeypatch):
    """最後一筆是角色開始、程式卻沒在跑：多半是中途停了。這時算剩餘時間沒有意義。"""
    current_env.alive = False
    current_env.write({"type": "character_start", "name": "bob", "target": 5})
    calls = []
    monkeypatch.setattr(b, "_seconds_per_image",
                        lambda events=None, **_kw: calls.append(1) or (30.0, True, "window", True))
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent[-1] == ("▶️ currently on **bob**\n"
                        "[" + "░" * 20 + "] `0/5` 0%  ⚠️ 背景產圖程式未在執行（可能已停止？）"), sent
    assert calls == []


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


@pytest.mark.parametrize("closing", ["todo_done", "critical_error"])
def test_current_treats_a_finished_or_failed_run_as_no_character(current_env, monkeypatch,
                                                                 closing):
    current_env.alive = False
    current_env.write({"type": "character_start", "name": "alice", "target": 3},
                      {"type": closing})
    sent = _replies(monkeypatch)
    _run(b.cmd_current(_message()))
    assert sent == ["目前沒有進行中的角色 — 背景產圖程式未在執行"], sent


# ---------------------------------------------------------------------------
# /out history
# ---------------------------------------------------------------------------
@pytest.fixture
def history_env(monkeypatch, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(b, "OUTPUT_ROOT", output)
    base = time.time() - 10_000

    def add(folder: str, name: str, age_rank: int):
        d = output / folder
        d.mkdir(exist_ok=True)
        p = d / name
        p.write_bytes(b"x")
        stamp = base + age_rank
        os.utime(p, (stamp, stamp))
        return p

    return types.SimpleNamespace(output=output, add=add)


def _history_rows(reply: str) -> list[str]:
    head, _, block = reply.partition("\n")
    assert block.startswith("```\n") and block.endswith("\n```"), reply
    return [line.split("  ", 1)[1] for line in block[4:-4].splitlines() if "  " in line]


def test_history_lists_the_newest_images_first(history_env, monkeypatch):
    """排序看的是 mtime，不是名字；只算角色資料夾裡的圖檔。

    列出來的是「資料夾/檔名」——這兩樣算不算 Layer 1 的專案路徑還沒有定論，
    哪天改掉顯示時這裡跟著改。"""
    history_env.add("alice", "z_old.png", 1)
    history_env.add("bob", "a_new.jpg", 3)
    history_env.add("alice", "m_mid.webp", 2)
    history_env.add("alice", "notes.txt", 9)
    (history_env.output / "stray.png").write_bytes(b"x")
    sent = _replies(monkeypatch)
    _run(b.cmd_history(_message(), ""))
    assert sent[-1].startswith("**history** — last 3 of 3:\n"), sent
    assert _history_rows(sent[-1]) == ["bob/a_new.jpg", "alice/m_mid.webp", "alice/z_old.png"]


@pytest.mark.parametrize("payload,shown", [("2", 2), ("0", 1), ("-5", 1), ("99", 30), ("", 10)])
def test_history_clamps_n_to_one_through_thirty(history_env, monkeypatch, payload, shown):
    for i in range(35):
        history_env.add("alice", f"img_{i:02d}.png", i)
    sent = _replies(monkeypatch)
    _run(b.cmd_history(_message(), payload))
    assert sent[-1].startswith(f"**history** — last {shown} of 35:"), sent
    rows = _history_rows(sent[-1])
    assert rows == [f"alice/img_{i:02d}.png" for i in range(34, 34 - shown, -1)]


@pytest.mark.parametrize("payload", ["abc", "1.5", "9" * 5000])
def test_history_rejects_a_non_integer_n(history_env, monkeypatch, payload):
    """`int()` 對超過 4300 位的數字丟 `ValueError`，跟打錯字走同一條「用法」。"""
    history_env.add("alice", "a.png", 1)
    sent = _replies(monkeypatch)
    _run(b.cmd_history(_message(), payload))
    assert sent == ["usage: `/out history [N]`"], sent


def test_history_with_no_images_says_so(history_env, monkeypatch):
    (history_env.output / "empty_char").mkdir()
    sent = _replies(monkeypatch)
    _run(b.cmd_history(_message(), "5"))
    assert sent == ["no images yet"], sent


def test_history_truncates_a_long_body_and_keeps_the_fence_closed(history_env, monkeypatch):
    for i in range(30):
        history_env.add("a" * 60, f"img_{i:02d}_" + "b" * 40 + ".png", i)
    history_env.add("c", "x```y.png", 99)
    sent = _replies(monkeypatch)
    _run(b.cmd_history(_message(), "30"))
    reply = sent[-1]
    assert reply.count("```") == 2, reply[-200:]
    assert "c/xʼʼʼy.png" in reply
    assert reply.endswith("\n…\n```") and len(reply) < 2000


# ---------------------------------------------------------------------------
# /preset show
# ---------------------------------------------------------------------------
@pytest.fixture
def preset_files(monkeypatch, tmp_path):
    names = {
        "PROMPT_FILE": "prompt.md",
        "TODO_PROMPT_FILE": "todo_prompt.md",
        "TODO_FILE_1": "todo_character1.md",
        "TODO_FILE_2": "todo_character2.md",
        "UNDESIRED_FILE": "undesired.md",
        "TODO_UNDESIRED_FILE": "todo_undesired.md",
    }
    paths = {}
    for const, name in names.items():
        paths[const] = tmp_path / name
        monkeypatch.setattr(b, const, paths[const])
    return paths


def test_prompt_info_shows_all_six_lists_in_order(preset_files, monkeypatch):
    """第一則用回覆、其餘五則直接送進頻道（一則一份，免得撞上單則長度上限）。"""
    preset_files["PROMPT_FILE"].write_text("masterpiece\n", encoding="utf-8")
    preset_files["TODO_FILE_2"].write_text("Amiya\n\nTexas\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    message = _message()
    _run(b.cmd_prompt_info(message))
    blocks = sent + message.channel.sent
    assert len(sent) == 1 and len(message.channel.sent) == 5
    assert blocks[0] == "**主提示詞預設**\n```\nmasterpiece\n\n```"
    assert blocks[1] == "**主提示詞佇列** _(empty)_"
    assert blocks[2] == "**角色1 佇列** _(empty)_"
    assert blocks[3] == "**角色2 佇列**\n```\nAmiya\n\nTexas\n\n```"
    assert blocks[4] == "**負面提示詞預設** _(empty)_"
    assert blocks[5] == "**負面提示詞佇列** _(empty)_"


def test_prompt_info_truncates_and_escapes_each_block(preset_files, monkeypatch):
    body = "x ``` y\n" + "z" * 3000
    preset_files["UNDESIRED_FILE"].write_text(body, encoding="utf-8")
    sent = _replies(monkeypatch)
    message = _message()
    _run(b.cmd_prompt_info(message))
    block = message.channel.sent[3]
    assert block.startswith("**負面提示詞預設**\n```\nx ʼʼʼ y\n")
    assert block.count("```") == 2 and len(block) < 2000
    more = len(body) - b.MAX_BLOCK_CHARS
    assert block.endswith(f"\n... (truncated; {more} more chars)\n```"), block[-80:]
    assert sent[0].endswith("_(empty)_")


def test_prompt_info_names_the_real_files_only_to_the_owner(preset_files, monkeypatch):
    sent = _replies(monkeypatch)
    owner = _message(b.OWNER_USER_ID)
    _run(b.cmd_prompt_info(owner))
    assert sent[0] == "**prompt.md** _(empty)_"
    assert owner.channel.sent[0] == "**todo_prompt.md** _(empty)_"
    stranger = _message()
    _run(b.cmd_prompt_info(stranger))
    assert all(".md" not in block for block in [sent[1], *stranger.channel.sent])


# ---------------------------------------------------------------------------
# /sys churn
# ---------------------------------------------------------------------------
def test_churn_scans_off_the_loop_from_local_midnight(monkeypatch):
    """掃描會跑好幾個 git 子行程，必須丟執行緒——在事件迴圈上跑會擋住心跳。"""
    seen = {}

    def _scan(since):
        seen["since"] = since
        seen["where"] = _where_called()
        return [("alpha", "\x1fA\n3\t1\tx.py\n\x1fB\n2\t0\ty.py\n"), ("beta", ""),
                ("gamma", None)]

    monkeypatch.setattr(b, "_scan_workspace_churn", _scan)
    sent = _replies(monkeypatch)
    _run(b.cmd_churn(_message()))
    assert seen["where"] == "thread", "工作區掃描跑在事件迴圈上"
    assert seen["since"] == b._local_midnight_since()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", seen["since"])
    assert sent[-1] == ("**今日 git 活動**（掃了 3 個資料夾）\n"
                        "提交 **2** · +5 / -1 行 · 2 位作者\n\n"
                        "- `alpha`：2 提交，+5 / -1 行\n"
                        "（另有 1 個資料夾讀不到，已略過）"), sent


def test_churn_with_no_commits_today_says_so(monkeypatch):
    monkeypatch.setattr(b, "_scan_workspace_churn", lambda since: [("alpha", "")])
    sent = _replies(monkeypatch)
    _run(b.cmd_churn(_message()))
    assert sent == ["今日還沒有任何 git 提交。"], sent


# ---------------------------------------------------------------------------
# /sys dashboard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("config,url", [
    ({"host": "0.0.0.0", "port": 9000}, "http://0.0.0.0:9000/"),
    ({}, "http://127.0.0.1:8765/"),
    ({"host": "", "port": 0}, "http://127.0.0.1:8765/"),
])
def test_dashboard_replies_with_the_configured_url_only(monkeypatch, config, url):
    """回的只有網址：啟動用的命令列含專案相對路徑，不得外送。"""
    monkeypatch.setattr(b, "DASHBOARD_CONFIG", config)
    sent = _replies(monkeypatch)
    _run(b.cmd_dashboard(_message()))
    assert sent == [f"dashboard: `{url}`"], sent


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
def _help_text(monkeypatch, payload: str) -> tuple[list, list]:
    sent = _replies(monkeypatch)
    message = _message()
    _run(b.cmd_help(message, payload))
    return sent, message.channel.sent


@pytest.mark.parametrize("payload,lang", [("tw", "zh-tw"), ("cn", "zh-cn"), ("EN", "en")])
def test_help_sends_the_requested_language_including_the_channel_sections(
        monkeypatch, payload, lang):
    """`/help` 一律附頻道限定的段落；每一塊都在單則長度上限內，接起來就是整份說明。"""
    sent, channel = _help_text(monkeypatch, payload)
    chunks = sent + channel
    assert len(sent) == 1, "第一塊要用回覆，其餘直接送"
    assert all(len(chunk) <= 1900 for chunk in chunks)
    joined = "\n".join(chunks)
    pack = b.HELPS[lang]
    for section in pack["channel"] + pack["mention"]:
        assert section in joined, section[:60]


@pytest.mark.parametrize("payload", ["", "klingon"])
def test_help_falls_back_to_the_default_language(monkeypatch, payload):
    sent, channel = _help_text(monkeypatch, payload)
    pack = b.HELPS[b.DEFAULT_HELP_LANG]
    assert sent[0].startswith(pack["channel"][0][:200]), sent[0][:120]


# ---------------------------------------------------------------------------
# /sys introspect_dom
# ---------------------------------------------------------------------------
def test_introspect_dom_writes_the_request_file_for_the_batch(monkeypatch, tmp_path):
    request = tmp_path / "dom_request.json"
    monkeypatch.setattr(b, "DOM_REQUEST_FILE", request)
    monkeypatch.setattr(b, "_webrunner_alive", lambda: True)
    sent = _replies(monkeypatch)
    _run(b.cmd_introspect_dom(_message()))
    assert json.loads(request.read_text(encoding="utf-8")) == {"cmd": "snapshot"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dom_request.json"], "暫存檔沒有收掉"
    assert sent[-1].startswith("📡 DOM 請求已寄出。"), sent


def test_introspect_dom_refuses_without_a_running_batch(monkeypatch, tmp_path):
    """沒有人會來讀那個請求檔：寫了只會留一個陳舊的請求，等下一次開批次時被當成新的。"""
    request = tmp_path / "dom_request.json"
    monkeypatch.setattr(b, "DOM_REQUEST_FILE", request)
    monkeypatch.setattr(b, "_webrunner_alive", lambda: False)
    sent = _replies(monkeypatch)
    _run(b.cmd_introspect_dom(_message()))
    assert not request.exists()
    assert sent == ["背景產圖程式沒在跑，無法 introspect DOM。先 `/run` 啟動再試。"], sent


def test_introspect_dom_reports_a_failed_write_without_the_path(monkeypatch, tmp_path, capsys):
    """寫不進去時，原始例外（帶主機路徑）只給擁有者；其他人拿泛用句。"""
    request = tmp_path / "missing_dir" / "dom_request.json"
    monkeypatch.setattr(b, "DOM_REQUEST_FILE", request)
    monkeypatch.setattr(b, "_webrunner_alive", lambda: True)
    sent = _replies(monkeypatch)
    _run(b.cmd_introspect_dom(_message()))
    assert sent == ["寫入請求失敗，請查看 log。"], sent
    assert "introspect_dom write failed" in capsys.readouterr().err
    _run(b.cmd_introspect_dom(_message(b.OWNER_USER_ID)))
    assert sent[-1].startswith("FileNotFoundError: "), sent


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


# ---------------------------------------------------------------------------
# /queue
# ---------------------------------------------------------------------------
@pytest.fixture
def queue_files(monkeypatch, tmp_path):
    paths = {const: tmp_path / name for const, name in (
        ("TODO_PROMPT_FILE", "todo_prompt.md"), ("TODO_FILE_1", "todo_character1.md"),
        ("TODO_FILE_2", "todo_character2.md"), ("TODO_UNDESIRED_FILE", "todo_undesired.md"))}
    for const, path in paths.items():
        monkeypatch.setattr(b, const, path)

    def write(const: str, body: str) -> None:
        paths[const].write_text(body, encoding="utf-8")

    return types.SimpleNamespace(paths=paths, write=write)


def _queue_embed(monkeypatch, message) -> tuple:
    seen: list = []

    async def _reply(_message, content=None, **kwargs):
        seen.append((content, kwargs))

    monkeypatch.setattr(b, "safe_reply", _reply)
    _run(b.cmd_queue(message))
    (content, kwargs), = seen
    assert content is None, "內容要放在 embed 裡"
    embed = kwargs["embed"]
    return embed, {field.name: field.value for field in embed.fields}


def test_queue_counts_each_list_and_the_longest_sets_the_pairs(queue_files, monkeypatch):
    """配對數是四個佇列裡最長的那一個（短的重複最後一筆）；角色2 的空白列是一筆位置資料，
    要算進去，否則顯示的配對數比實際跑的少。"""
    queue_files.write("TODO_PROMPT_FILE", "a\nb\nc\n")
    queue_files.write("TODO_FILE_1", "x\n")
    queue_files.write("TODO_FILE_2", "y\n\n\nz\n")
    queue_files.write("TODO_UNDESIRED_FILE", "n1\nn2\n")
    embed, fields = _queue_embed(monkeypatch, _message())
    assert fields == {"主提示詞佇列": "`3`", "角色1 佇列": "`1`", "角色2 佇列": "`4`",
                      "負面提示詞佇列": "`2`", "pairs (max)": "**4**"}
    assert embed.color.value == 0x57F287 and embed.footer.text is None


def test_queue_counts_the_negative_prompt_queue_as_work(queue_files, monkeypatch):
    """負面提示詞佇列一樣會拉長配對數（批次那一側的配對規則是四個佇列取最長）；只有它有內容
    時批次照樣會跑，不能顯示成空佇列。"""
    queue_files.write("TODO_UNDESIRED_FILE", "n1\nn2\nn3\n")
    embed, fields = _queue_embed(monkeypatch, _message())
    assert fields["pairs (max)"] == "**3**"
    assert embed.color.value == 0x57F287 and embed.footer.text is None


def test_queue_stops_counting_at_the_end_marker(queue_files, monkeypatch):
    queue_files.write("TODO_PROMPT_FILE", "a\nb\n END \nc\n")
    queue_files.write("TODO_FILE_1", "\n".join(f"c{i}" for i in range(6)) + "\n")
    _embed, fields = _queue_embed(monkeypatch, _message())
    assert fields["pairs (until `end`)"] == "**2** / 6"
    assert fields["⏹ end marker"] == "主提示詞佇列第 #3 筆 — run stops there"
    assert "pairs (max)" not in fields


@pytest.mark.parametrize("prompt", [None, "end\na\n"])
def test_queue_with_nothing_to_run_is_red_and_says_so(queue_files, monkeypatch, prompt):
    """四個都空，或第一筆就是 `end`：實際一組都不會跑。"""
    if prompt is not None:
        queue_files.write("TODO_PROMPT_FILE", prompt)
        queue_files.write("TODO_FILE_1", "x\n")
    embed, fields = _queue_embed(monkeypatch, _message())
    assert embed.color.value == 0xED4245
    assert embed.footer.text == "queue empty — 背景程式會用 fallback / no-op"
    if prompt is None:
        assert fields["pairs (max)"] == "**0**"
    else:
        assert fields["pairs (until `end`)"] == "**0** / 2"


def test_queue_names_the_real_files_only_to_the_owner(queue_files, monkeypatch):
    _embed, fields = _queue_embed(monkeypatch, _message(b.OWNER_USER_ID))
    assert {"todo_prompt.md", "todo_character1.md", "todo_character2.md",
            "todo_undesired.md"} <= set(fields)
    _embed, fields = _queue_embed(monkeypatch, _message())
    assert not any(name.endswith(".md") for name in fields)
