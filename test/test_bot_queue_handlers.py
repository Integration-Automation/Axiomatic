"""會改佇列檔或讀設定檔的指令處理函式。

`/todo prompt end`、`/todo prompt unend`、`/todo <佇列> pop`、`/todo <佇列> clear` 都會
**改寫佇列檔**，而批次在每個角色邊界都會重讀那幾個檔——寫錯一筆，後面每一對都配錯，而且
沒有任何錯誤訊息。所以這裡斷言的是**寫完之後磁碟上的內容**、`/sys undo` 拿得回原本內容，
以及沒有動作時檔案一個位元組都沒變，不只是「回了一句話」。

`/config show` 唯讀，但它是使用者判斷「我設的值有沒有生效」的唯一入口，所以要釘住每一個
可設定的鍵都列出來、預設與覆寫分得開。

所有佇列檔、`.backup/`、undo 堆疊與設定檔都換到 `tmp_path`，一個正式檔都不碰。
"""
from __future__ import annotations

import asyncio
import collections
import json
import types
from pathlib import Path

import pytest

import _batch_config as bc
import discord_bot as b


def _run(coro, timeout: float = 10.0):
    """跑一段 async 本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _replies(monkeypatch) -> list:
    """把 `safe_reply` 換成記錄器，回 `[(content, kwargs), …]`。"""
    sent: list = []

    async def _reply(_message, content=None, **kwargs):
        sent.append((content, kwargs))

    monkeypatch.setattr(b, "safe_reply", _reply)
    return sent


_STRANGER = types.SimpleNamespace(author=types.SimpleNamespace(id=12345))


def _owner():
    return types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))


@pytest.fixture
def queues(monkeypatch, tmp_path):
    """四個佇列檔、`.backup/` 與 undo 堆疊全部換到 `tmp_path`。

    undo 堆疊要換一份新的：`_safe_write` 會往模組層的堆疊推一筆，留在那裡的話後面某支
    測試的 `/sys undo` 會撿去還原一個早就不存在的暫存檔。"""
    paths = types.SimpleNamespace(
        prompt=tmp_path / "todo_prompt.md",
        char1=tmp_path / "todo_character1.md",
        char2=tmp_path / "todo_character2.md",
        neg=tmp_path / "todo_undesired.md",
        backup=tmp_path / ".backup",
    )
    monkeypatch.setattr(b, "TODO_PROMPT_FILE", paths.prompt)
    monkeypatch.setattr(b, "TODO_FILE_1", paths.char1)
    monkeypatch.setattr(b, "TODO_FILE_2", paths.char2)
    monkeypatch.setattr(b, "TODO_UNDESIRED_FILE", paths.neg)
    monkeypatch.setattr(b, "BACKUP_DIR", paths.backup)
    monkeypatch.setattr(b, "_UNDO_STACK", collections.deque(maxlen=50))
    return paths


def _backups(paths) -> list[Path]:
    return sorted(paths.backup.glob("*.bak")) if paths.backup.exists() else []


# ---------------------------------------------------------------------------
# /todo prompt end
# ---------------------------------------------------------------------------
def test_tp_end_appends_one_marker_and_keeps_an_undo_copy(queues, monkeypatch):
    """`end` 加在最後一筆之後；回覆講它是第幾筆、前面還會跑幾組。改動走 `_safe_write`，
    所以 `/sys undo` 拿得回原本的內容。"""
    queues.prompt.write_text("a\nb\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_end(_STRANGER))
    assert queues.prompt.read_text(encoding="utf-8") == "a\nb\nend\n"
    content, _kw = sent[-1]
    assert content.startswith("added `end` stop-marker at entry #3 of **主提示詞佇列**"), sent
    assert "stop after the 2 pair(s) before it" in content
    assert len(b._UNDO_STACK) == 1
    original, backup = b._UNDO_STACK[-1]
    assert original == queues.prompt
    assert backup.read_text(encoding="utf-8") == "a\nb\n"


@pytest.mark.parametrize("marker", ["end", "END", "  End  "])
def test_tp_end_refuses_a_second_marker_without_writing(queues, monkeypatch, marker):
    """已經有一個（不分大小寫、前後空白不算）就不再加——兩個 `end` 不會讓批次停得更早，
    只會讓 `/todo prompt unend` 回報的筆數對不上使用者以為的。檔案要一個位元組都沒動。"""
    body = f"a\n{marker}\nb\n".encode("utf-8")
    queues.prompt.write_bytes(body)
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_end(_STRANGER))
    assert queues.prompt.read_bytes() == body
    assert _backups(queues) == [] and len(b._UNDO_STACK) == 0
    content, _kw = sent[-1]
    assert content.startswith("an `end` marker is already at entry #2 of **主提示詞佇列**"), sent
    assert "(after 1 pair(s))" in content and "`/todo prompt unend`" in content


def test_tp_end_on_a_missing_queue_creates_it_with_only_the_marker(queues, monkeypatch):
    """佇列檔還不存在＝空佇列：寫出只有一行 `end` 的檔，前面 0 組。undo 存的是「原本
    不存在」的哨兵，還原時才知道要刪檔而不是寫回空字串。"""
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_end(_STRANGER))
    assert queues.prompt.read_text(encoding="utf-8") == "end\n"
    assert "entry #1 of" in sent[-1][0] and "after the 0 pair(s)" in sent[-1][0]
    _original, backup = b._UNDO_STACK[-1]
    assert backup.read_text(encoding="utf-8") == b._UNDO_SENTINEL_ABSENT


def test_tp_end_names_the_real_file_only_to_the_owner(queues, monkeypatch):
    """佇列的真實檔名對擁有者照實講（擁有者例外），其他人只看到泛用標籤。"""
    queues.prompt.write_text("a\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_end(_owner()))
    assert "**todo_prompt.md**" in sent[-1][0], sent
    queues.prompt.write_text("a\n", encoding="utf-8")
    _run(b.cmd_tp_end(_STRANGER))
    assert "todo_prompt.md" not in sent[-1][0] and "主提示詞佇列" in sent[-1][0], sent


# ---------------------------------------------------------------------------
# /todo prompt unend
# ---------------------------------------------------------------------------
def test_tp_unend_removes_every_marker_and_keeps_the_order(queues, monkeypatch):
    """每一個 `end`（不分大小寫）都拿掉，其餘照原順序留下。`endless`、`the end` 這種
    只是含有那個字的提示詞不是停止標記，拿掉它們就是刪了使用者的資料。"""
    queues.prompt.write_text("a\nEND\nendless\n  end \nthe end\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_unend(_STRANGER))
    assert queues.prompt.read_text(encoding="utf-8") == "a\nendless\nthe end\n"
    content, _kw = sent[-1]
    assert content.startswith("removed 2 `end` markers from **主提示詞佇列** (3 entries left)"), sent
    _original, backup = b._UNDO_STACK[-1]
    assert backup.read_text(encoding="utf-8") == "a\nEND\nendless\n  end \nthe end\n"


def test_tp_unend_says_marker_in_the_singular(queues, monkeypatch):
    queues.prompt.write_text("a\nend\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_unend(_STRANGER))
    assert queues.prompt.read_text(encoding="utf-8") == "a\n"
    assert sent[-1][0].startswith("removed 1 `end` marker from "), sent


@pytest.mark.parametrize("body", [None, "", "a\nendless\n"])
def test_tp_unend_without_a_marker_writes_nothing(queues, monkeypatch, body):
    """沒有標記就不寫：多寫一次會多一筆 undo 備份，`/sys undo` 就會先還原一個什麼都沒改的
    版本，使用者以為撤回了上一個動作，其實沒有。"""
    if body is not None:
        queues.prompt.write_text(body, encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_tp_unend(_STRANGER))
    assert sent[-1][0] == "no `end` marker in **主提示詞佇列** — nothing to remove", sent
    assert _backups(queues) == [] and len(b._UNDO_STACK) == 0
    if body is None:
        assert not queues.prompt.exists()
    else:
        assert queues.prompt.read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
# /todo <佇列> pop
# ---------------------------------------------------------------------------
def test_pop_removes_the_last_entry_and_shows_it(queues, monkeypatch):
    queues.char1.write_text("Amiya\nTexas\nLappland\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_pop(_STRANGER, queues.char1))
    assert queues.char1.read_text(encoding="utf-8") == "Amiya\nTexas\n"
    assert sent[-1][0] == (
        "popped last entry from **角色1 佇列** (2 left)\n```\nLappland\n```"), sent
    _original, backup = b._UNDO_STACK[-1]
    assert backup.read_text(encoding="utf-8") == "Amiya\nTexas\nLappland\n"


def test_pop_on_character_two_keeps_the_positional_blank_rows(queues, monkeypatch):
    """角色2 佇列是位置式的：空白列＝「那一對不要角色2」。讀寫任何一邊把空白列濾掉，
    後面每一對都會往前錯位。"""
    queues.char2.write_text("Amiya\n\nTexas\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_pop(_STRANGER, queues.char2))
    assert queues.char2.read_text(encoding="utf-8") == "Amiya\n\n"
    assert "(2 left)" in sent[-1][0] and "```\nTexas\n```" in sent[-1][0], sent


def test_pop_on_character_two_takes_a_trailing_blank_row_as_the_last_entry(queues, monkeypatch):
    """最後一筆本身是空白列時，被拿掉的就是它——它是一筆有位置意義的資料，不是格式。"""
    queues.char2.write_text("Amiya\nTexas\n\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_pop(_STRANGER, queues.char2))
    assert queues.char2.read_text(encoding="utf-8") == "Amiya\nTexas\n"
    assert sent[-1][0].endswith("(2 left)\n```\n\n```"), sent


def test_pop_of_an_empty_queue_writes_nothing(queues, monkeypatch):
    queues.neg.write_text("\n  \n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_pop(_STRANGER, queues.neg))
    assert sent[-1][0] == "**負面提示詞佇列** is empty; nothing to pop", sent
    assert queues.neg.read_text(encoding="utf-8") == "\n  \n"
    assert _backups(queues) == []


def test_pop_preview_is_truncated_and_cannot_close_the_fence(queues, monkeypatch):
    """被拿掉的那一筆是使用者貼的文字，可以合法地含 ```；不跳脫的話外面的區塊會提前關掉。"""
    long_entry = "x ``` y " + "z" * 300
    queues.prompt.write_text(f"keep\n{long_entry}\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_pop(_STRANGER, queues.prompt))
    reply = sent[-1][0]
    assert reply.count("```") == 2, reply
    assert "x ʼʼʼ y " in reply and reply.endswith("…\n```")
    assert "z" * 121 not in reply


# ---------------------------------------------------------------------------
# /todo <佇列> clear
# ---------------------------------------------------------------------------
def test_clear_empties_the_file_and_can_be_undone(queues, monkeypatch):
    """清空是留下一個 0 位元組的檔（不是刪檔、也不是寫一個換行——那會被讀成一筆空白列，
    在角色2 佇列裡等於「第一對不要角色2」）。"""
    queues.char2.write_text("Amiya\n\nTexas\n", encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_character_clear(_STRANGER, queues.char2))
    assert queues.char2.exists() and queues.char2.read_bytes() == b""
    assert sent[-1][0] == "cleared **角色2 佇列**", sent
    original, backup = b._UNDO_STACK[-1]
    assert original == queues.char2
    assert backup.read_text(encoding="utf-8") == "Amiya\n\nTexas\n"


def test_clear_writes_only_the_queue_it_was_given(queues, monkeypatch):
    for path in (queues.prompt, queues.char1, queues.char2, queues.neg):
        path.write_text("keep\n", encoding="utf-8")
    _replies(monkeypatch)
    _run(b.cmd_character_clear(_STRANGER, queues.char1))
    assert queues.char1.read_bytes() == b""
    for path in (queues.prompt, queues.char2, queues.neg):
        assert path.read_text(encoding="utf-8") == "keep\n", path.name


# ---------------------------------------------------------------------------
# /config show
# ---------------------------------------------------------------------------
@pytest.fixture
def batch_config(monkeypatch, tmp_path):
    path = tmp_path / "batch_config.json"
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", path)
    monkeypatch.setattr(bc, "_BATCH_CONFIG_TMP", tmp_path / "batch_config.json.tmp")
    return path


def _config_rows(sent) -> dict[str, str]:
    """`/config show` 的 embed → `{鍵: 那一行鍵名之後的文字}`。"""
    _content, kwargs = sent[-1]
    embed = kwargs["embed"]
    desc = embed.description
    assert desc.startswith("```\n") and desc.endswith("\n```"), desc
    rows = {}
    for line in desc[len("```\n"):-len("\n```")].splitlines():
        key, _, rest = line.partition(" ")
        rows[key] = rest.strip()
    return rows


def test_config_show_lists_every_settable_key_and_flags_the_defaults(
        batch_config, monkeypatch):
    """寫在檔案裡的鍵沒有 `(default)`，沒寫的有；值的寫法跟 `/config set` 收的一樣
    （區間用 en dash、布林小寫），使用者才能照抄回去。"""
    batch_config.write_text(json.dumps({
        "images_per_character": 50,
        "inter_image_delay_sec": [3, 5.5],
        "debug_screenshots": True,
    }), encoding="utf-8")
    sent = _replies(monkeypatch)
    _run(b.cmd_config(_STRANGER))
    rows = _config_rows(sent)
    assert list(rows) == list(b._BATCH_SETTERS), list(rows)
    assert rows["images_per_character"] == "50"
    assert rows["inter_image_delay_sec"] == "3–5.5"
    assert rows["debug_screenshots"] == "true"
    defaults = bc._DEFAULT_BATCH_CONFIG
    assert rows["rest_hours"] == f"{b._fmt_cfg_value(defaults['rest_hours'])}  (default)"
    flagged = {key for key, rest in rows.items() if rest.endswith("(default)")}
    assert flagged == set(b._BATCH_SETTERS) - {
        "images_per_character", "inter_image_delay_sec", "debug_screenshots"}
    embed = sent[-1][1]["embed"]
    assert embed.title == "⚙️ batch 設定"
    assert "/config set <key> <value>" in embed.footer.text
    assert sent[-1][0] is None, "內容要放在 embed 裡，不是另外再送一段文字"


def test_config_show_without_a_file_shows_every_default(batch_config, monkeypatch):
    sent = _replies(monkeypatch)
    _run(b.cmd_config(_STRANGER))
    rows = _config_rows(sent)
    for key, rest in rows.items():
        expected = b._fmt_cfg_value(bc._DEFAULT_BATCH_CONFIG[key])
        assert rest == f"{expected}  (default)", (key, rest)
    assert not batch_config.exists(), "唯讀的指令建立了設定檔"


def test_config_show_never_writes_the_file(batch_config, monkeypatch):
    body = '{"rest_hours": 4}'
    batch_config.write_text(body, encoding="utf-8")
    _replies(monkeypatch)
    _run(b.cmd_config(_STRANGER))
    assert batch_config.read_text(encoding="utf-8") == body
    assert sorted(p.name for p in batch_config.parent.iterdir()) == ["batch_config.json"]
