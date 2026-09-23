"""磁碟上的 todo 佇列格式契約（`CLAUDE.md` DoD #6 → "todo file format"）。

規則寫得很細，卻**只有 webrunner 那一側被測到**（`test_webrunner_shared.
test_read_todo_characters_normalises` 一支）。bot 那一側的 `read_todo_entries` /
`write_todo_entries` 一個測試都沒有——而契約文字明明白白同時點名兩者。

這個缺口不是理論上的。實際踩到的：`/todo dedupe char2` 用
``e.strip().lower()`` 當 key 去重，於是**每一列空白列的 key 都是 ""**，第二列以
後全被當成「重複項」刪掉。但角色2 佇列是位置式的，空白列代表「這一對不要
Character 2」；刪掉一列，後面每一列往前位移，之後每一對都配到錯的角色。bot 還
會回一句「removed 1 duplicate」告訴使用者成功了。錯誤要等幾小時後看產出資料夾
才發現。

所以這支測試做三件事：

1. **兩側讀取器餵相同位元組必須得到相同結果**——契約是跨行程的，只驗一側等於
   沒驗。
2. **寫入器的三條不變量**（保留角色2 位置空白列、非空剛好一個結尾換行、空清單
   寫 0 byte），兩側都驗，並且互相 round-trip。
3. **沒有任何 bot 的批次改寫會弄掉位置空白列**——行為驗證擋住已修的那個 bug，
   AST 列舉則強迫「新增一個會寫佇列的指令」時必須先寫下它對位置語意的判斷。
"""
import ast
import asyncio
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _webrunner_shared as ws          # noqa: E402
import discord_bot as b                 # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


# --------------------------------------------------------------------------
# 兩側讀取器：同樣的位元組必須讀成同樣的清單
# --------------------------------------------------------------------------
def _read_both(raw: str, tmp_path: Path, *, positional: bool,
               monkeypatch) -> tuple[list[str], list[str]]:
    """把 `raw` 寫進暫存檔，分別用 bot 與 webrunner 的讀取器讀回來。

    bot 那側靠 ``path == TODO_FILE_2`` **自動**判斷要不要保留空白列，webrunner
    那側靠明確的 ``preserve_blank=`` 關鍵字——兩種完全不同的決定方式，正是最容易
    悄悄分岔的地方，所以這裡刻意用各自原生的呼叫慣例。
    """
    path = tmp_path / ("todo_character2.md" if positional else "todo_prompt.md")
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(b, "TODO_FILE_2",
                        path if positional else tmp_path / "not-char2.md")
    return (b.read_todo_entries(path),
            ws.read_todo_characters(path, preserve_blank=positional))


# 契約的三條規則各給一組資料，正反都有。
_CONTRACT_CASES = [
    # (名稱, 檔案內容, 位置式?, 期望結果)
    ("只用換行切分、逗號留在條目裡",
     "1girl, solo, (masterpiece)\nAmiya，明日方舟\n", False,
     ["1girl, solo, (masterpiece)", "Amiya，明日方舟"]),
    ("雙冒號與括號都不是分隔符號",
     "artist::name (series)\nb\n", False, ["artist::name (series)", "b"]),
    ("一般佇列跳過空白列與純空白列",
     "a\n\n   \nb\n", False, ["a", "b"]),
    ("角色2 佇列保留位置空白列",
     "Amiya\n\nTexas\n", True, ["Amiya", "", "Texas"]),
    ("角色2 的純空白列也算位置空白列",
     "Amiya\n \t \nTexas\n", True, ["Amiya", "", "Texas"]),
    ("NBSP 在條目內部正規化成一般空格",
     "1girl,\xa0solo\n", False, ["1girl, solo"]),
    ("整列只有 NBSP 時算空白列",
     "a\n\xa0\nb\n", False, ["a", "b"]),
    ("條目首尾空白會被 strip",
     "  a  \n\tb\t\n", False, ["a", "b"]),
    ("空檔讀成空清單", "", False, []),
    ("角色2 空檔也讀成空清單、不是單一空字串", "", True, []),
    ("沒有結尾換行的最後一列照樣算一筆", "a\nb", False, ["a", "b"]),
    # 讀取端用 `str.splitlines()`，所以換行不只 `\n`。這一列把最容易被忽略的一個
    # 放進契約表；完整清單的逐一驗證在下面「寫入端保證一筆寫進去、一筆讀回來」。
    ("Unicode 行分隔字元（U+2028）兩側都當換行",
     "a b\n", False, ["a", "b"]),
]


@pytest.mark.parametrize("name,raw,positional,expected", _CONTRACT_CASES,
                         ids=[case[0] for case in _CONTRACT_CASES])
def test_both_readers_honour_the_contract(name, raw, positional, expected,
                                          tmp_path, monkeypatch):
    bot_result, runner_result = _read_both(
        raw, tmp_path, positional=positional, monkeypatch=monkeypatch)
    assert bot_result == expected, f"bot 側讀錯（{name}）"
    assert runner_result == expected, f"webrunner 側讀錯（{name}）"


def test_a_missing_file_reads_as_empty_on_both_sides(tmp_path, monkeypatch):
    """佇列檔不存在是正常狀態（使用者還沒建），不能丟例外。"""
    missing = tmp_path / "todo_prompt.md"
    monkeypatch.setattr(b, "TODO_FILE_2", tmp_path / "not-char2.md")
    assert b.read_todo_entries(missing) == []
    assert ws.read_todo_characters(missing) == []


def test_neither_reader_ever_splits_on_a_comma(tmp_path, monkeypatch):
    """契約第 1 條的反向驗證：任何一種逗號都不得產生額外的條目。"""
    for comma in (",", "，", "、", ", "):
        raw = f"left{comma}right\n"
        bot_result, runner_result = _read_both(
            raw, tmp_path, positional=False, monkeypatch=monkeypatch)
        assert len(bot_result) == 1, f"bot 被 {comma!r} 切開了：{bot_result}"
        assert len(runner_result) == 1, (
            f"webrunner 被 {comma!r} 切開了：{runner_result}")


# --------------------------------------------------------------------------
# 兩側寫入器：三條不變量
# --------------------------------------------------------------------------
def _write_both(entries: list[str], tmp_path: Path,
                monkeypatch) -> tuple[str, str]:
    """回傳 (bot 寫出的位元組, webrunner 寫出的位元組)。"""
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    bot_path = tmp_path / "bot.md"
    runner_path = tmp_path / "runner.md"
    b.write_todo_entries(bot_path, entries)
    ws.write_todo_characters(runner_path, entries)
    return (bot_path.read_text(encoding="utf-8"),
            runner_path.read_text(encoding="utf-8"))


_WRITE_CASES = [
    ("非空清單以換行連接並補剛好一個結尾換行", ["a", "b"], "a\nb\n"),
    ("單筆也要有結尾換行", ["a"], "a\n"),
    ("空清單寫成 0 byte、不是一個換行", [], ""),
    ("角色2 的位置空白列原樣寫回", ["A", "", "B", "", "C"], "A\n\nB\n\nC\n"),
    ("結尾的位置空白列不會被吃掉", ["A", ""], "A\n\n"),
    ("條目裡的逗號原樣保留", ["1girl, solo"], "1girl, solo\n"),
    ("全形逗號、雙冒號與括號原樣保留",
     ["Amiya，明日方舟", "artist::name (series)"],
     "Amiya，明日方舟\nartist::name (series)\n"),
]


@pytest.mark.parametrize("name,entries,expected", _WRITE_CASES,
                         ids=[case[0] for case in _WRITE_CASES])
def test_both_writers_honour_the_contract(name, entries, expected,
                                          tmp_path, monkeypatch):
    bot_bytes, runner_bytes = _write_both(entries, tmp_path, monkeypatch)
    assert bot_bytes == expected, f"bot 側寫錯（{name}）"
    assert runner_bytes == expected, f"webrunner 側寫錯（{name}）"


def test_a_non_empty_write_never_ends_with_two_newlines(tmp_path, monkeypatch):
    """「剛好一個結尾換行」的反向驗證。

    多一個換行 = 佇列尾端多一個空條目，在角色2 佇列就是憑空多出一對「不要
    Character 2」的圖。
    """
    bot_bytes, runner_bytes = _write_both(["a", "b"], tmp_path, monkeypatch)
    for label, text in (("bot", bot_bytes), ("webrunner", runner_bytes)):
        assert not text.endswith("\n\n"), f"{label} 寫出了多餘的結尾換行"


@pytest.mark.parametrize("entries", [
    ["Amiya", "", "Texas", "", "Exusiai"],
    ["a"],
    [],
    ["1girl, solo, (masterpiece:1.2)", "artist::name"],
    ["Amiya，明日方舟", "", "artist::name (series)"],
])
def test_the_two_sides_round_trip_each_other(entries, tmp_path, monkeypatch):
    """真正的跨行程契約：bot 寫的 webrunner 要讀得回來，反之亦然。

    這兩支函式住在不同行程、不同模組，中間只有磁碟。任何一側改了格式而另一側
    沒跟上，症狀都是「安靜地讀成別的東西」而不是崩潰。
    """
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    positional = any(not entry.strip() for entry in entries)
    path = tmp_path / "todo_character2.md"
    monkeypatch.setattr(b, "TODO_FILE_2",
                        path if positional else tmp_path / "not-char2.md")

    b.write_todo_entries(path, entries)
    assert ws.read_todo_characters(path, preserve_blank=positional) == entries, (
        "bot 寫的 webrunner 讀不回來")

    ws.write_todo_characters(path, entries)
    assert b.read_todo_entries(path) == entries, "webrunner 寫的 bot 讀不回來"


# --------------------------------------------------------------------------
# bot 的 preserve_blank 是「路徑相等」判斷——把它的語意釘住
# --------------------------------------------------------------------------
def test_the_bot_decides_positional_by_path_equality_not_by_filename(
        tmp_path, monkeypatch):
    """`read_todo_entries` 用 ``path == TODO_FILE_2`` 決定要不要保留空白列。

    這是**路徑相等**，不是「檔名叫 todo_character2.md」。等價但不同物件的 Path
    可以，但**別處**一個同名檔案不行——備份副本、暫存副本、範本目錄裡的同名
    檔，讀進來都會靜靜地掉光位置空白列。webrunner 那側沒有這個陷阱（它要求呼叫
    端明講 `preserve_blank=`）。

    釘住它有兩個作用：任何人想改成比對檔名會立刻紅；想沿用這個自動判斷去讀非
    正本檔案的人，會在這裡讀到為什麼不行。
    """
    real = tmp_path / "todo_character2.md"
    real.write_text("A\n\nB\n", encoding="utf-8")
    monkeypatch.setattr(b, "TODO_FILE_2", real)

    assert b.read_todo_entries(real) == ["A", "", "B"]
    # 等價的另一個 Path 物件仍然相等，所以仍然保留空白列。
    assert b.read_todo_entries(Path(str(real))) == ["A", "", "B"]

    # 同名、不同目錄 → 不相等 → 空白列被丟掉。這是現況，不是值得推廣的行為；
    # 要讀非正本的角色2 檔案請明講 `preserve_blank=True`。
    elsewhere = tmp_path / "copy" / "todo_character2.md"
    elsewhere.parent.mkdir()
    elsewhere.write_text("A\n\nB\n", encoding="utf-8")
    assert b.read_todo_entries(elsewhere) == ["A", "B"]
    assert b.read_todo_entries(elsewhere, preserve_blank=True) == ["A", "", "B"]


# --------------------------------------------------------------------------
# 沒有任何批次改寫可以弄掉位置空白列
# --------------------------------------------------------------------------
class _FakeMessage:
    """只用來收 bot 的回覆；測試不看措辭，只確認有回、且不會炸。"""

    def __init__(self):
        self.replies: list[str] = []

    async def reply(self, text, **_kwargs):
        self.replies.append(text)


def _char2_fixture(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "todo_character2.md"
    path.write_text("Amiya\n\nTexas\n\nExusiai\n", encoding="utf-8")
    monkeypatch.setattr(b, "TODO_FILE_2", path)
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setitem(b._LIST_ALIASES, "char2", path)
    return path


def _blank_rows(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if not line.strip())


@pytest.mark.parametrize("command,payload", [
    ("cmd_dedupe", "char2"),
    ("cmd_shuffle", "char2"),
])
def test_bulk_mutations_keep_every_positional_blank_row(
        command, payload, tmp_path, monkeypatch):
    """整批改寫（去重、洗牌）不得改變空白列的**數量**。

    `/todo dedupe char2` 曾經違反這條：空白列的 dedupe key 都是 ""，第二列以後
    被當成重複項刪掉，後面每一對都配到錯的角色，而 bot 回報「removed 1
    duplicate」。現在它偵測到位置空白列就拒做並說明原因。
    """
    path = _char2_fixture(tmp_path, monkeypatch)
    before = _blank_rows(path)
    assert before == 2, "測試資料本身要有位置空白列，否則這支測試什麼都沒驗到"

    message = _FakeMessage()
    asyncio.run(getattr(b, command)(message, payload))

    assert _blank_rows(path) == before, (
        f"`{command}` 弄掉了角色2 佇列的位置空白列（{before} → "
        f"{_blank_rows(path)}）。空白列代表「這一對不要 Character 2」，少一列就"
        f"會讓後面每一列往前位移、每一對都配到錯的角色，而且不會有任何錯誤"
        f"訊息。見 CLAUDE.md → todo file format。")
    assert message.replies, f"`{command}` 沒有回覆使用者"


def test_dedupe_refuses_a_positional_char2_queue_and_says_why(
        tmp_path, monkeypatch):
    """拒做的時候要講清楚，否則使用者只會以為指令壞了。"""
    path = _char2_fixture(tmp_path, monkeypatch)
    message = _FakeMessage()
    asyncio.run(b.cmd_dedupe(message, "char2"))

    assert path.read_text(encoding="utf-8") == "Amiya\n\nTexas\n\nExusiai\n"
    reply = "\n".join(message.replies)
    assert "空白" in reply and "/todo remove" in reply, (
        f"拒做的訊息要說明是空白列擋下來的、也要給替代做法；實際回覆：{reply!r}")


def test_dedupe_still_works_where_there_is_nothing_positional(
        tmp_path, monkeypatch):
    """守衛只針對「真的在用位置語意」的角色2 佇列，不能順手把去重整個廢掉。"""
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setattr(b, "TODO_FILE_2", tmp_path / "todo_character2.md")

    # 沒有空白列的角色2 佇列照常去重。
    char2 = tmp_path / "todo_character2.md"
    char2.write_text("Texas\nAmiya\ntexas\n", encoding="utf-8")
    monkeypatch.setitem(b._LIST_ALIASES, "char2", char2)
    asyncio.run(b.cmd_dedupe(_FakeMessage(), "char2"))
    assert b.read_todo_entries(char2) == ["Texas", "Amiya"]

    # 其他佇列不受影響。
    prompt = tmp_path / "todo_prompt.md"
    prompt.write_text("a\nb\nA\n", encoding="utf-8")
    monkeypatch.setitem(b._LIST_ALIASES, "prompt", prompt)
    asyncio.run(b.cmd_dedupe(_FakeMessage(), "prompt"))
    assert b.read_todo_entries(prompt) == ["a", "b"]


@pytest.mark.parametrize("command, content, expect", [
    ("cmd_dedupe", "a\nb\n", "no duplicates"),
    ("cmd_shuffle", "a\n", "nothing to shuffle"),
])
def test_a_bulk_command_with_nothing_to_do_does_not_rewrite_the_queue(
        command, content, expect, tmp_path, monkeypatch):
    """沒有重複項的去重、只有一筆的洗牌，都不該寫檔。批次正在同一份佇列上逐筆取用：
    讀進來再原樣寫回去的那一瞬間，批次若剛好取走一筆，這次寫入會把它**寫回來**，
    同一筆就會再產一次。寫入器換成絆線，只要被叫到就失敗。"""
    prompt = tmp_path / "todo_prompt.md"
    prompt.write_text(content, encoding="utf-8")
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setitem(b._LIST_ALIASES, "prompt", prompt)
    writes = []
    monkeypatch.setattr(b, "write_todo_entries", lambda *a, **k: writes.append(a))
    message = _FakeMessage()
    asyncio.run(getattr(b, command)(message, "prompt"))
    assert writes == [], f"`{command}` 在沒事可做時改寫了佇列"
    assert expect in "\n".join(message.replies), message.replies


@pytest.mark.parametrize("command, payload, expect", [
    ("cmd_move", "prompt 1 top", "already at the top"),
    ("cmd_move", "prompt 3 bottom", "already at the bottom"),
    ("cmd_swap", "prompt 2 2", "nothing to do"),
])
def test_moving_an_entry_to_where_it_already_is_does_not_rewrite_the_queue(
        command, payload, expect, tmp_path, monkeypatch):
    """理由同上一支。`/todo swap` 的 i == j 一直有擋，`/todo move` 把第一筆移到最上面、
    最後一筆移到最下面卻照樣寫檔（2026-09-23 補上）。"""
    prompt = tmp_path / "todo_prompt.md"
    prompt.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setitem(b._LIST_ALIASES, "prompt", prompt)
    writes = []
    monkeypatch.setattr(b, "write_todo_entries", lambda *a, **k: writes.append(a))
    message = _FakeMessage()
    asyncio.run(getattr(b, command)(message, payload))
    assert writes == [], f"`{command} {payload}` 在沒事可做時改寫了佇列"
    assert expect in "\n".join(message.replies), message.replies


def test_a_real_move_still_writes(tmp_path, monkeypatch):
    """上面那道只擋原地不動的情形：往另一端移照樣要寫。"""
    prompt = tmp_path / "todo_prompt.md"
    prompt.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setitem(b._LIST_ALIASES, "prompt", prompt)
    asyncio.run(b.cmd_move(_FakeMessage(), "prompt 1 bottom"))
    assert b.read_todo_entries(prompt) == ["b", "c", "a"]
    asyncio.run(b.cmd_move(_FakeMessage(), "prompt 3 top"))
    assert b.read_todo_entries(prompt) == ["a", "b", "c"]


@pytest.mark.parametrize("command", ["cmd_dedupe", "cmd_shuffle"])
def test_a_bulk_command_on_an_unknown_list_writes_nothing(command, monkeypatch):
    writes = []
    monkeypatch.setattr(b, "write_todo_entries", lambda *a, **k: writes.append(a))
    message = _FakeMessage()
    asyncio.run(getattr(b, command)(message, "no_such_list"))
    assert writes == []
    assert "unknown list" in "\n".join(message.replies), message.replies


# --------------------------------------------------------------------------
# 新增佇列改寫指令時，強迫先寫下它對位置語意的判斷
# --------------------------------------------------------------------------
# key = `discord_bot.py` 裡會呼叫 `write_todo_entries` 的函式名；
# value = 它為什麼不會弄掉角色2 的位置空白列。
_REVIEWED_MUTATORS = {
    "write_todo_entries":
        "寫入器本身：原樣寫出呼叫端給的清單，不做任何過濾。",
    "cmd_character_add":
        "只 append，既有列的位置不動。",
    "cmd_character_clear":
        "整份清空是使用者明確要求的動作，位置語意本來就一起沒了。",
    "cmd_character_remove":
        "刪的是使用者指名的那一列；位移是他要的，不是意外。",
    "cmd_character_pop":
        "刪最後一列；後面沒有列可以位移，位置語意不受影響。",
    "cmd_move":
        "pop + insert，長度不變、空白列只是換位置。",
    "cmd_swap":
        "對調兩列，長度與空白列數量都不變。",
    "cmd_duplicate":
        "只 insert，不刪任何列。",
    "cmd_shuffle":
        "重排，空白列**數量**不變；角色2 佇列另外在回覆裡提醒標記換了位置。",
    "cmd_dedupe":
        "偵測到角色2 佇列有位置空白列就拒做並說明——這條規則就是它違反過才有的。",
    "_cmd_push_default":
        "只 append 一筆；多行的來源整個拒絕，不會被拆成多筆。",
    "cmd_tp_end":
        "只作用在主提示詞佇列（沒有位置空白列），且只 append。",
    "cmd_tp_insert":
        "只作用在主提示詞佇列，且只 insert（一次可插入多筆，依序放在指定位置）。",
    "cmd_tp_unend":
        "只作用在主提示詞佇列；濾掉的是 `end` 標記，不是空白列。",
    # --- 產圖批次那一側（2026-09-09 補；理由見 `_todo_writers` 的 docstring）---
    "write_todo_characters":
        "寫入器本身：原樣寫出呼叫端給的清單，不做任何過濾（與 bot 那側的 "
        "`write_todo_entries` 對稱）。",
    "run_batch":
        "只在 `end` 標記那條路寫檔，而且只寫**主提示詞佇列**——那個佇列沒有位置"
        "空白列（空白列只在 `todo_character2.md` 有語意）。做的事是把第一個 `end` "
        "行 pop 掉，其餘原樣寫回。",
    "_pop_cursor":
        "在游標位置 pop 一筆。角色2 佇列由呼叫端傳 `preserve_blank=True`，而且 pop "
        "之前有 front-match 守衛（游標那筆不等於剛消耗的 entry 就不動），所以使用者"
        "手動重排／刪除時不會被蓋掉。長度只減一、其餘列原樣寫回。",
}

# 「會寫佇列檔」的判準。判準是**呼叫哪個寫入器**，不是模組——bot 用
# `write_todo_entries`、產圖批次用 `write_todo_characters`，兩邊寫的是同一批檔案，
# 所以規則也只有一條（`CLAUDE.md` 的 todo 契約同時綁住兩邊）。
_TODO_WRITER_PREFIX = "write_todo_"
_MODULE_FLOOR = 25          # 抽不到檔案時「零筆」跟「全部乾淨」長得一模一樣


def _project_module_asts():
    """專案自己的**非測試**模組 → `[(檔名, AST)]`。用 glob 而不是列舉：列舉是
    fail-open 的，下一個新模組不會自動被蓋到。`legacy/` 不含在內。"""
    root = PKG_ROOT.parent
    out = []
    # `test/` 照同一個判準過濾：`conftest.py` 與手動 e2e 腳本在 2026-09-22 之前住在
    # 套件裡、在範圍內，搬家之後照舊。
    for path in (sorted(PKG_ROOT.glob("*.py")) + sorted((root / "test").glob("*.py"))
                 + sorted(root.glob("*.py"))):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        try:
            out.append((path.name,
                        ast.parse(path.read_text(encoding="utf-8"), path.name)))
        except (SyntaxError, UnicodeDecodeError):   # pragma: no cover
            continue
    return out


def _todo_writers(trees) -> dict:
    """`[(模組名, AST)]` → `{函式名: 模組名}`——定義或呼叫任何 `write_todo_*` 的函式。

    **這一支 2026-09-09 從「掃 `discord_bot.py` 的原始碼子字串」改成「掃全專案的
    AST 呼叫節點」，兩個方向都是實際的缺口：**

    * **範圍**：規則是 `CLAUDE.md` 的 todo 契約，而那份契約明文同時綁住 bot 的
      `write_todo_entries` 與產圖批次的 `write_todo_characters`。舊版只掃
      `discord_bot.py`，於是 `_webrunner_shared.py` 的兩個寫入點
      （`run_batch` 的 `end` 標記消耗、`_pop_cursor`）**從來沒有被要求列管**——
      而 `_pop_cursor` 正是唯一會動到 `todo_character2.md` 的地方，也就是位置空白列
      唯一有語意的那個佇列。這是「規則跟模組無關、
      範圍卻寫死一個檔案」的又一個實例。
    * **判準**：舊版是 `"write_todo_entries" in ast.unparse(node)`，也就是**子字串**
      ——一個只在 docstring 裡提到寫入器名字的函式會被算成寫入器。本 repo 對這個形狀
      有明文結論（用 AST 不要用子字串），這裡是漏網的一處。

    歸屬取**最內層**的函式：`_pop_cursor` 是巢狀在 `run_batch` 裡的，兩邊都算的話
    `run_batch` 會被要求為一件它自己沒做的事寫理由，而那種理由只會誤導下一個人。
    """
    found: dict[str, str] = {}

    def visit(node, current, module):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith(_TODO_WRITER_PREFIX):
                    found.setdefault(child.name, module)
                visit(child, child, module)
                continue
            if (isinstance(child, ast.Call)
                    and getattr(child.func, "id", "").startswith(
                        _TODO_WRITER_PREFIX)
                    and current is not None):
                found.setdefault(current.name, module)
            visit(child, current, module)

    for module, tree in trees:
        visit(tree, None, module)
    return found


def _todo_writer_names() -> set[str]:
    return set(_todo_writers(_project_module_asts()))


def test_every_todo_writer_has_a_recorded_verdict_on_positional_blanks():
    """會寫佇列檔的函式必須逐一列管，連「為什麼安全」一起寫下來。

    行為測試只擋得住我想得到的指令；這條擋的是**下一個**指令。新增一個會呼叫
    `write_todo_entries` 的函式時，這支測試會紅，逼你先想清楚它碰到角色2 的位置
    空白列會怎樣——那正是 `/todo dedupe` 當初沒人想過的事。
    """
    actual = _todo_writer_names()
    missing = actual - set(_REVIEWED_MUTATORS)
    stale = set(_REVIEWED_MUTATORS) - actual

    assert not missing, (
        "這些函式會寫 todo 佇列檔，但沒有列進 `_REVIEWED_MUTATORS`："
        + "、".join(sorted(missing))
        + "。請先確認它對角色2 的位置空白列做了什麼（空白列 = 「這一對不要 "
          "Character 2」，刪一列就讓後面全部位移），再把結論寫進去。")
    assert not stale, (
        "`_REVIEWED_MUTATORS` 裡這些函式已經不存在了，清掉："
        + "、".join(sorted(stale)))


def test_the_verdicts_are_actually_written_out():
    """列管清單要有內容才有意義，不能只是把名字塞進去讓測試變綠。"""
    for name, reason in _REVIEWED_MUTATORS.items():
        assert len(reason.strip()) > 10, f"{name} 的理由太短，寫清楚為什麼安全"


def test_the_writer_scan_actually_reaches_both_sides():
    """範圍 pin：這條契約綁的是**兩側**，掃描就必須看得到兩側。

    真實資料乾淨時，「掃全專案」與「只掃 `discord_bot.py`」的違規清單都是空的
    ——兩者在輸出上分不出來。所以直接量族群：
    寫入器必須來自一個以上的模組，而且產圖批次那一側一定要在裡面（唯一會動到
    `todo_character2.md` 的 `_pop_cursor` 就住在那裡，而位置空白列只在那個佇列
    有語意）。
    """
    trees = _project_module_asts()
    assert len(trees) >= _MODULE_FLOOR, (
        f"只抽到 {len(trees)} 個模組，抽取器壞了——這支守門會退化成永遠會過。")
    writers = _todo_writers(trees)
    assert len(writers) >= 10, (
        f"只找到 {sorted(writers)} 這些寫入器，抽取器多半壞了。")
    modules = set(writers.values())
    assert len(modules) >= 2, (
        f"所有寫入器都來自同一個模組（{modules}）——掃描範圍被縮窄了，還是"
        "產圖批次那一側的寫入點被搬走了？兩種都要人看一眼。")
    assert "_webrunner_shared.py" in modules, (
        f"掃描沒看到產圖批次那一側（實際：{sorted(modules)}）。"
        "`_pop_cursor` 是唯一會動到 `todo_character2.md` 的地方。")


def test_the_writer_scan_looks_at_calls_not_at_source_text():
    """合成對照組：判準是 AST 的呼叫節點，不是原始碼子字串。

    舊版寫 `"write_todo_entries" in ast.unparse(node)`，於是**只在 docstring 裡提到
    寫入器名字**的函式也會被算成寫入器。本 repo 對這個形狀早有明文結論（用 AST 不要
    用子字串），這裡是漏網的一處。乾淨的原始碼讓上面兩支永遠綠，所以每一種形狀都得
    自己造。
    """
    source = textwrap.dedent('''
        def calls_it():
            write_todo_entries(p, xs)

        def calls_the_other_one():
            write_todo_characters(p, xs)

        def only_mentions_it():
            """這裡只是提到 write_todo_entries 而已，沒有真的呼叫。"""
            return 1

        def write_todo_something(p, xs):
            return None
        ''')
    got = _todo_writers([("synthetic.py", ast.parse(source))])
    assert set(got) == {"calls_it", "calls_the_other_one",
                        "write_todo_something"}, sorted(got)


def test_a_nested_writer_is_attributed_to_the_innermost_function():
    """歸屬取最內層——外層不該被要求為一件它自己沒做的事寫理由。

    這不是假設：`_webrunner_shared._pop_cursor` 真的是巢狀在 `run_batch` 裡的，而
    `run_batch` 另外有自己的（`end` 標記）寫入點。兩者的理由完全不同，混在一起寫
    只會誤導下一個人。
    """
    source = textwrap.dedent('''
        def outer():
            def inner(p, xs):
                write_todo_characters(p, xs)
            return inner
        ''')
    got = _todo_writers([("synthetic.py", ast.parse(source))])
    assert set(got) == {"inner"}, sorted(got)


def _fake_corpus(writer_defs_per_module: dict, filler: int = 30):
    """做一份合成語料：`{模組名: 這個模組裡有幾個 `write_todo_*` 定義}` ＋ 一堆空模組。

    給下面那幾支「下限對照組」用。空模組是為了讓**模組數**那一道下限先通過，這樣
    後面那幾道才輪得到執行——三道下限是依序的，只餵一個空清單只證得了第一道。
    """
    trees = [(f"filler_{i}.py", ast.parse("")) for i in range(filler)]
    for module, count in writer_defs_per_module.items():
        source = "\n".join(f"def write_todo_fake_{i}(p, xs):\n    return None"
                           for i in range(count))
        trees.append((module, ast.parse(source)))
    return trees


def test_the_writer_floor_fires_when_the_enumerator_comes_back_empty(monkeypatch):
    """下限自己的對照組：抽不到模組時那兩道斷言都必須真的會叫。

    真實的 `_project_module_asts()` 本來就回幾十個檔案，所以下限在正常情況下永遠
    成立、改不改都看不出差別——而它們存在的唯一理由正是「不正常的那一天」，那一天的
    症狀就是「零筆違規」。
    """
    monkeypatch.setattr(sys.modules[__name__], "_project_module_asts", lambda: [])
    with pytest.raises(AssertionError) as scope:
        test_the_writer_scan_actually_reaches_both_sides()
    assert "抽取器壞了" in str(scope.value), scope.value

    with pytest.raises(AssertionError) as roster:
        test_every_todo_writer_has_a_recorded_verdict_on_positional_blanks()
    assert "已經不存在了" in str(roster.value), roster.value


def test_the_two_remaining_floors_each_fire_on_their_own(monkeypatch):
    """另外兩道下限也要各自的對照組——**依序的斷言只有第一道會被上面那支證到**。

    這是變異測試逼出來的：上面那支把語料換成空的，於是「模組數」那一道先炸，後面
    「寫入器數量」與「來自幾個模組」兩道**一次都沒執行過**。把它們分別放寬成 0，
    六支測試照樣全綠。一支控制測試只證得了它真的跑到的那一行。
    """
    mod = sys.modules[__name__]

    # (1) 模組數夠、寫入器太少 → 「抽取器多半壞了」那一道要叫。
    monkeypatch.setattr(mod, "_project_module_asts",
                        lambda: _fake_corpus({"only_one.py": 3}))
    with pytest.raises(AssertionError) as few:
        test_the_writer_scan_actually_reaches_both_sides()
    assert "抽取器多半壞了" in str(few.value), few.value

    # (2) 寫入器夠多、但全部擠在同一個模組 → 「範圍被縮窄了」那一道要叫。
    monkeypatch.setattr(mod, "_project_module_asts",
                        lambda: _fake_corpus({"only_one.py": 12}))
    with pytest.raises(AssertionError) as one_module:
        test_the_writer_scan_actually_reaches_both_sides()
    assert "掃描範圍被縮窄了" in str(one_module.value), one_module.value


# --------------------------------------------------------------------------
# 寫入端保證一筆寫進去、一筆讀回來
# --------------------------------------------------------------------------
def _splitlines_boundaries() -> list[str]:
    """`str.splitlines()` 認得的每一個換行字元——掃 0..0x10FFFF 全部碼位推出來。

    兩側讀取器都用 `splitlines()` 切，而它認的換行不只 `\\n`。這裡刻意**不列表**：
    手列的表只反映寫表的人記得哪些，而漏掉的那一個正是會讓一筆變成兩筆的那一個。
    只掃 0–0x3000 再補 U+2028/U+2029 也會得到同一份答案，但那等於先假設了答案；
    全掃在這台機器上約 0.2 秒，而且只在收集時跑一次。
    """
    return [chr(cp) for cp in range(0x110000)
            if len(f"a{chr(cp)}b".splitlines()) > 1]


_LINE_BOUNDARIES = _splitlines_boundaries()
# `\r\n` 是**一個**換行、兩個字元，單字元掃描抓不到它，另外補上。
_BOUNDARY_CASES = _LINE_BOUNDARIES + ["\r\n"]
_BOUNDARY_IDS = ["CRLF" if len(c) > 1 else f"U+{ord(c):04X}"
                 for c in _BOUNDARY_CASES]


def test_the_boundary_derivation_found_what_splitlines_documents():
    """推導本身的下限。

    參數化清單若是空的，pytest 只回報一筆「空參數集」的 skip，**不會變紅**——下面
    三支會安靜地什麼都沒驗。量過的答案是 10 個；少於這個數代表推導壞了（例如掃描
    範圍被縮窄），不是 Python 改了定義。
    """
    assert len(_LINE_BOUNDARIES) >= 10, _LINE_BOUNDARIES
    assert {"\n", "\r", "\x0b", "\x1c", "\x85", " ", " "} <= set(
        _LINE_BOUNDARIES), _LINE_BOUNDARIES


@pytest.mark.parametrize("boundary", _BOUNDARY_CASES, ids=_BOUNDARY_IDS)
def test_both_readers_split_on_every_boundary_the_writer_refuses(
        boundary, tmp_path, monkeypatch):
    """寫入端拒絕的**前提**：這些字元兩側讀取器都真的會切。

    寫入端的判斷問的是 `str.splitlines`，不是讀取器本身；兩者一致才讓那個判斷是對的
    判斷。哪天某一側改成只切 `\\n`，這裡會紅——寫入端那道檢查就從「必要」變成「過嚴」
    ，要有人重新想一次。
    """
    bot_result, runner_result = _read_both(
        f"left{boundary}right\n", tmp_path, positional=False,
        monkeypatch=monkeypatch)
    assert bot_result == ["left", "right"], bot_result
    assert runner_result == ["left", "right"], runner_result


@pytest.mark.parametrize("shape", ["a{}b", "a{}", "{}a", "{}"],
                         ids=["middle", "trailing", "leading", "alone"])
@pytest.mark.parametrize("boundary", _BOUNDARY_CASES, ids=_BOUNDARY_IDS)
def test_the_writer_refuses_an_entry_that_would_read_back_as_several(
        boundary, shape, tmp_path, monkeypatch):
    """含換行的一筆會被讀回成好幾筆，所以寫入端必須在碰磁碟**之前**拒絕。

    用角色2 佇列當場景，因為那裡的代價最高：它是位置式的，多一筆就讓後面每一對
    都配到錯的角色，而且不會有任何錯誤訊息。拒絕必須是整批的——原檔一個位元組都
    不變，`.backup/` 也不能多出東西（備份在 `_safe_write` 裡建立，比它早就擋下
    才叫 fail-closed）。
    """
    backup_dir = tmp_path / ".backup"
    monkeypatch.setattr(b, "BACKUP_DIR", backup_dir)
    path = tmp_path / "todo_character2.md"
    before = "Amiya\n\nTexas\n".encode("utf-8")
    path.write_bytes(before)

    with pytest.raises(ValueError) as refused:
        b.write_todo_entries(path, ["Amiya", "", shape.format(boundary), "Texas"])

    assert path.read_bytes() == before, "拒絕之後原檔被改了"
    assert not backup_dir.exists(), "拒絕之前就先做了備份——檢查的位置太晚"
    assert "#3" in str(refused.value), (
        f"訊息要指出是第幾筆，才查得到是誰給的：{refused.value}")
    assert str(tmp_path) not in str(refused.value), "訊息不得帶路徑"


@pytest.mark.parametrize("char", ["\t", "\x1b", "\x1f", "​", "‧",
                                  " "],
                         ids=["TAB", "ESC", "U+001F", "U+200B", "U+2027",
                              "U+202F"])
def test_look_alike_characters_still_round_trip(char, tmp_path, monkeypatch):
    """反面對照組：長得像分隔符號、但**不是** `splitlines` 換行的字元必須照收。

    這一組專挑緊鄰真換行的鄰居——`\\x1f` 挨著 `\\x1c`–`\\x1e`、U+2027 挨著
    U+2028、TAB 是空白但不換行。只驗「會拒絕」的話，「把所有控制字元或所有空白都
    擋掉」的過嚴實作也會全綠。
    """
    assert char not in _LINE_BOUNDARIES
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setattr(b, "TODO_FILE_2", tmp_path / "not-char2.md")
    path = tmp_path / "todo_prompt.md"
    entry = f"a{char}b"

    b.write_todo_entries(path, [entry, "c"])

    assert b.read_todo_entries(path) == [entry, "c"]
    assert ws.read_todo_characters(path) == [entry, "c"]


def _prompt_fixture(tmp_path: Path, monkeypatch,
                    content: str = "a\nb\nc\n") -> Path:
    path = tmp_path / "todo_prompt.md"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(b, "TODO_PROMPT_FILE", path)
    monkeypatch.setattr(b, "TODO_FILE_2", tmp_path / "todo_character2.md")
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    return path


def _reported_total(reply: str) -> int:
    found = re.search(r"\((\d+) total\)", reply)
    assert found, f"回覆裡找不到總筆數：{reply!r}"
    return int(found.group(1))


@pytest.mark.parametrize("text", ["x\ny", "x\\y", "x y", "x\r\ny",
                                  "x\x85y"],
                         ids=["LF", "backslash", "U+2028", "CRLF", "U+0085"])
def test_insert_splits_like_add_and_reports_the_count_on_disk(
        text, tmp_path, monkeypatch):
    """`/todo prompt insert` 的內容照 `/todo prompt add` 的規則拆，依序插在指定位置。

    舊版把整段當成**一筆**插進去：回覆說「(4 total)」，檔案讀回來卻是五筆——佇列
    一行一筆，換行在讀取時就把它切開了。回覆的總數必須等於磁碟上的筆數。
    """
    path = _prompt_fixture(tmp_path, monkeypatch)
    message = _FakeMessage()

    asyncio.run(b.cmd_tp_insert(message, f"2 {text}"))

    after = b.read_todo_entries(path)
    assert after == ["a", "x", "y", "b", "c"]
    assert _reported_total(message.replies[-1]) == len(after)
    assert "#2–#3" in message.replies[-1], message.replies[-1]


def test_insert_of_a_single_entry_is_unchanged(tmp_path, monkeypatch):
    """正面對照組：單一筆的插入跟以前一模一樣（位置、總數、回覆形狀）。"""
    path = _prompt_fixture(tmp_path, monkeypatch)
    message = _FakeMessage()

    asyncio.run(b.cmd_tp_insert(message, "2 x"))

    assert b.read_todo_entries(path) == ["a", "x", "b", "c"]
    assert _reported_total(message.replies[-1]) == 4
    assert "inserted at #2 of" in message.replies[-1], message.replies[-1]


def test_insert_can_append_several_at_the_next_free_slot(tmp_path, monkeypatch):
    """`index` 可以是「最後一筆 + 1」——多筆時也要依序接在尾端，不是倒過來。"""
    path = _prompt_fixture(tmp_path, monkeypatch)

    asyncio.run(b.cmd_tp_insert(_FakeMessage(), "4 x\\y\\z"))

    assert b.read_todo_entries(path) == ["a", "b", "c", "x", "y", "z"]


def test_insert_of_nothing_but_separators_changes_nothing(tmp_path, monkeypatch):
    """拆完什麼都不剩（只有分隔符號）→ 沿用「沒有東西可插」的回覆，檔案不動。"""
    path = _prompt_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    message = _FakeMessage()

    asyncio.run(b.cmd_tp_insert(message, "2 \\ \\"))

    assert path.read_bytes() == before
    assert "nothing to insert" in message.replies[-1], message.replies[-1]


@pytest.mark.parametrize("index", ["0", "-1", "5"])
def test_insert_refuses_an_index_outside_the_queue(index, tmp_path, monkeypatch):
    """範圍外的編號要拒絕、檔案一個位元組都不動。

    這一邊在分支覆蓋率裡從來沒有成立過（2026-09-22），而拿掉它不會炸，只會**插錯
    地方**：`0`（照 0 起算的習慣打）落到 `entries[-1:-1]`，新的一筆靜靜插在**倒數第一筆
    前面**，回覆卻寫「#0」；`5` 會被切片默默改成接在尾端。佇列是會被批次逐筆消化的
    資料，插錯位置沒有任何症狀。"""
    path = _prompt_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    message = _FakeMessage()

    asyncio.run(b.cmd_tp_insert(message, f"{index} x"))

    assert path.read_bytes() == before
    assert "out of range (1..4" in message.replies[-1], message.replies[-1]


def test_insert_accepts_both_ends_of_the_range(tmp_path, monkeypatch):
    """對照：`1` 與「最後一筆 + 1」都在範圍內——擋差一的改法。"""
    path = _prompt_fixture(tmp_path, monkeypatch)
    asyncio.run(b.cmd_tp_insert(_FakeMessage(), "1 first"))
    asyncio.run(b.cmd_tp_insert(_FakeMessage(), "5 last"))
    assert b.read_todo_entries(path) == ["first", "a", "b", "c", "last"]


def _push_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    templates = tmp_path / "templates"
    templates.mkdir()
    monkeypatch.setattr(b, "TEMPLATES_DIR", templates)
    dest = tmp_path / "todo_character2.md"
    dest.write_text("A\n\nB\n", encoding="utf-8")
    monkeypatch.setattr(b, "TODO_FILE_2", dest)
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    return templates, dest


@pytest.mark.parametrize("source_name", ["two_lines.md", "character2.md"],
                         ids=["template", "default-file"])
@pytest.mark.parametrize("body", ["Texas\nAmiya", "Texas Amiya",
                                  "Texas\n\nAmiya\n"],
                         ids=["LF", "U+2028", "blank-line-between"])
def test_push_default_refuses_a_multi_line_source(
        body, source_name, tmp_path, monkeypatch):
    """多行的預設檔／範本整個拒絕：不拆、不接，目的佇列一個位元組都不動。

    佇列一行一筆，多行的來源推進去會被讀回成好幾筆；在角色2 佇列那就是後面每一對
    都位移。回覆只能用泛用標籤（範本名或 `_list_label`），不得帶路徑。
    """
    templates, dest = _push_fixture(tmp_path, monkeypatch)
    folder = templates if source_name == "two_lines.md" else tmp_path
    source = folder / source_name
    source.write_text(body, encoding="utf-8")
    before = dest.read_bytes()
    message = _FakeMessage()

    asyncio.run(b._cmd_push_default(message, source, dest))

    assert dest.read_bytes() == before, "多行來源被推進去了"
    assert not (tmp_path / ".backup").exists(), "拒絕的路上不該留下備份"
    reply = "\n".join(message.replies)
    assert message.replies, "拒絕時沒有回覆使用者"
    for leak in (str(tmp_path), str(source), ".md"):
        assert leak not in reply, f"回覆帶出了路徑或檔名：{reply!r}"


def test_push_default_still_pushes_a_single_line_source(tmp_path, monkeypatch):
    """正面對照組：單行來源（含結尾換行）照常推成一筆。

    少了這一支，「一律拒絕」的實作也會讓上面那支全綠。
    """
    templates, dest = _push_fixture(tmp_path, monkeypatch)
    source = templates / "one_line.md"
    source.write_text("Texas, (arknights)\n", encoding="utf-8")

    asyncio.run(b._cmd_push_default(_FakeMessage(), source, dest))

    assert b.read_todo_entries(dest) == ["A", "", "B", "Texas, (arknights)"]


def main() -> int:
    """獨立執行入口（不經過 pytest）。"""
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
