"""每一個「壞檔就退回預設、絕不往外拋」的載入器，遇到**不是 UTF-8** 的檔也要成立。

`UnicodeDecodeError` 是 **`ValueError` 的子類別，不是 `OSError`**。所以
`except OSError` 接不住它——而本專案幾乎每一個載入器原本都只寫了 `OSError`
（加上 `FileNotFoundError` / `json.JSONDecodeError`），於是 docstring 上那句
「never raises」對「檔案不是 UTF-8」並不成立。

**這在這台機器上不是理論問題。** locale 是 cp950，而這些檔案的內容幾乎都含中文：
記事本以外的編輯器另存成 Big5、從別處貼進來的內容、或一次半寫入正好切在多位元組
字元中間，都會產生解不開的位元組。CLAUDE.md 已經有「文字 I/O 一律指名編碼」這條
硬規則並由 `test_text_encoding.py` 靜態守著——但那條守的是「有沒有寫 `encoding=`」，
指名之後**讀到不符合的位元組會拋**才是這一份在守的下一步。

三個後果依嚴重度排：

1. `load_bot_config` / `load_batch_config` 在**模組匯入期**就被呼叫
   （`discord_bot.py`、`dorossi_backend.py`、webrunner 每個角色一次），拋出去等於
   **bot 起不來**、批次跑到一半死掉，而且 traceback 指向 `read_text`，看起來像
   「檔案讀不到」而不是「編碼不對」。
2. `_chrome_slot.read_holder` 在取槽路徑上，拋出去等於一次半寫入的鎖檔就讓整個
   spawn 炸掉——而那正是這把鎖要處理的情況。
3. 其餘（進度檢查點、presence 設定、RPC 設定）拋出去會讓對應的背景迴圈整支結束，
   而迴圈結束的外顯症狀是「狀態不再更新」，跟「現在沒事發生」分不出來。

**刻意不用 `errors="replace"`。** 那會把亂碼當成有效值繼續用下去——一個被誤讀的
目錄路徑或角色名比「退回預設」糟得多，後者至少是已知且可預期的行為。
"""
from __future__ import annotations


import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _batch_config as bc  # noqa: E402
import _bot_config as bo  # noqa: E402
import _chrome_slot as cs  # noqa: E402
import _run_progress as rp  # noqa: E402
import discord_bot as b  # noqa: E402
import discord_rpc as rpc  # noqa: E402
import presence_probe as pp  # noqa: E402
import _webrunner_shared as ws  # noqa: E402

# Big5 的「設定檔」——`\xb3` 是 UTF-8 的連續位元組，不能當起始位元組，所以這串
# 一定解不開。用真實會發生的編碼（不是隨機位元組）比較貼近實際情形。
UNDECODABLE = '{"note": "設定檔"}'.encode("big5")


def test_the_fixture_really_is_undecodable():
    """守門的自我檢查。這串要是哪天解得開，下面每一支都會變成空轉的綠燈。"""
    with pytest.raises(UnicodeDecodeError):
        UNDECODABLE.decode("utf-8")


def test_a_decode_error_is_not_an_os_error():
    """整份測試的前提。`except OSError` 接不住它——這就是缺陷的根。"""
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, OSError)


# ---------------------------------------------------------------------------
# 每一個載入器：壞編碼 → 回預設，不得拋
# ---------------------------------------------------------------------------

def test_the_bot_config_falls_back(tmp_path, monkeypatch, capsys):
    """這支在 `discord_bot` / `dorossi_backend` 的**模組層**被呼叫。

    拋出去不是「這個設定讀不到」，是**整個 bot 起不來**——而重啟是 supervisor
    自動做的，所以會變成重生迴圈。
    """
    path = tmp_path / "bot_config.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", path)
    cfg = bo.load_bot_config()
    assert cfg == bo._fallback_bot_config()
    assert "read failed" in capsys.readouterr().err


def test_the_batch_config_falls_back(tmp_path, monkeypatch, capsys):
    """webrunner 每個角色重讀一次，所以這支拋出去是**批次跑到一半死掉**。"""
    path = tmp_path / "batch_config.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", path)
    cfg = bc.load_batch_config()
    assert cfg == dict(bc._DEFAULT_BATCH_CONFIG)
    assert "read failed" in capsys.readouterr().err


def test_the_raw_batch_config_falls_back(tmp_path, monkeypatch):
    """`read_raw_batch_config` 的合約是「壞檔 → 空 dict，never raises」。

    它的呼叫端會拿回傳值**整檔覆寫**，所以拋出去至少不會寫壞東西；但回空 dict
    也不是安全的預設——寫回去等於清空設定。這裡只釘住「不拋」，寫入端的保護
    在 `_atomic_write_config` 那一側。
    """
    path = tmp_path / "batch_config.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", path)
    assert bc.read_raw_batch_config() == {}


def test_the_chrome_slot_holder_falls_back(tmp_path, monkeypatch):
    """這支在取槽路徑上。拋出去 ＝ 一次半寫入的鎖檔就讓整個 spawn 炸掉。

    **回什麼也有講究，2026-09-09 改過一次。** 原本回 `None`，而 `None` 在
    `_chrome_slot` 裡的意思是「檔案不存在」——於是 `acquire()` 會去重試 `O_EXCL`
    建立、必然失敗，槽被永久卡住，同時 `held_by_live_other()` 回 False 連「忙線中」
    都不說。現在跟「空檔／JSON 壞掉」走同一條路，回一個以 mtime 當 `acquired_at`
    的 holder，讓時間 backstop 在 `DEFAULT_STALE_AFTER_SEC` 之後把它搶走。

    這支測試真正要釘的仍然是**不得往外拋**；下面兩個斷言把「回什麼」也一起釘住，
    免得下次有人「順手」改回 `None`。
    """
    path = tmp_path / ".chrome_slot.lock"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(cs, "LOCK_PATH", path)
    holder = cs.read_holder()                      # 不得 raise
    assert holder is not None, (
        "解不開的鎖檔回了 None＝『沒有人持有』，但檔案就在那裡——"
        "`acquire()` 會永遠拿不到這個槽，而且不會有任何訊息說明原因。")
    assert holder["pid"] is None and holder["acquired_at"] == path.stat().st_mtime


def test_the_resume_checkpoint_falls_back(tmp_path, monkeypatch):
    """回 None ＝ 從頭開始。那是可接受的結果；拋出去不是。"""
    path = tmp_path / "run_progress.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(rp, "PROGRESS_FILE", path)
    assert rp.read_progress() is None


def test_the_rpc_config_falls_back(tmp_path, monkeypatch, capsys):
    path = tmp_path / "presence_rpc.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(rpc, "RPC_CONFIG_FILE", path)
    cfg = rpc.load_rpc_config()
    assert cfg["enabled"] is False
    assert "read" in capsys.readouterr().err


@pytest.mark.parametrize("const, loader", [
    ("GAMES_FILE", "_load_game_whitelist"),
    ("MUSIC_FILE", "_load_music_rules"),
    ("RPC_CONFIG_FILE", "_load_claude_detection"),
])
def test_the_presence_configs_fall_back(tmp_path, monkeypatch, capsys,
                                        const, loader):
    """presence 的三個設定檔各自有一個載入器，三個都要成立。

    拋出去會讓 presence 迴圈整支結束，而那的外顯症狀是「狀態不再更新」——跟
    「現在沒事發生」分不出來。
    """
    path = tmp_path / "cfg.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(pp, const, path)
    getattr(pp, loader)()          # 不得拋
    assert "failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# discord_bot 的五處：不是「載入器回預設」，是五種不同的退路
# ---------------------------------------------------------------------------
# 上面那批全是設定載入器，形狀一致（回預設 + 印一行）。bot 這五處刻意各走各的路，
# 因為「解不開的時候該當作什麼」每一處的正確答案不一樣——照抄同一個退路才是錯的。

def test_the_prompt_fallback_reads_as_empty(tmp_path):
    """`prompt.md` / `character{1,2}.md` / `undesired.md` 是**使用者手編**的檔。

    手編 ＝ 隨時可能被別的編輯器另存成 Big5，而這台機器的 locale 正是 cp950。
    拋出去會讓 `/gen plan`、`/queue`、`/eta` 這些佇列預覽整支爆掉；回空字串等於
    「這個 fallback 沒東西」，跟檔案不存在走同一條既有的路。
    """
    path = tmp_path / "prompt.md"
    path.write_bytes(UNDECODABLE)
    assert b._fallback_text(path) == ""


def test_the_batch_label_reads_as_empty(tmp_path, monkeypatch):
    """這個檔是**跨行程**的：bot 寫、儀表板讀。

    所以「一次半寫入正好切在多位元組字元中間」不是假想情況，而是這類檔案的
    常態失敗形態。回空字串 ＝ 「這一輪沒有標籤」。
    """
    path = tmp_path / "batch_label.txt"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(b, "BATCH_LABEL_FILE", path)
    assert b._get_batch_label() == ""


def test_the_declared_dependencies_read_as_none(tmp_path, monkeypatch):
    """`requirements.txt` 解不開 → 回空清單 ＝ 這一輪不做「宣告了但沒裝」檢查。

    方向是**保守**的：回空不會誤報缺套件，只是少做一項檢查。拋出去則是整個
    `/sys doctor` 掛掉——一個診斷指令因為診斷資料本身壞掉而消失，最沒有幫助。
    """
    (tmp_path / "requirements.txt").write_bytes(UNDECODABLE)
    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    assert b._declared_dependencies() == []


def test_the_undo_refuses_an_undecodable_backup(tmp_path, monkeypatch, capsys):
    """這一支**特別不能**用 `errors="replace"` 混過去。

    讀到的內容會被原封不動**寫回原檔**，所以把亂碼讀成「有效內容」等於用 undo
    把佇列弄壞——比「這筆 undo 做不到」糟得多，而且不可逆。正確行為是拒絕、
    把備份留在原地。

    順便釘住兩件事：原始例外只進 stderr，送到 Discord 的是泛用句（假訊息的作者
    不是擁有者，走 `_owner_error` 的泛用那一半）。
    """
    original = tmp_path / "todo_prompt.md"
    backup = tmp_path / "todo_prompt.bak"
    original.write_text("keep me\n", encoding="utf-8")
    backup.write_bytes(UNDECODABLE)

    replies: list[str] = []

    async def _fake_reply(_message, content=None, **_kwargs):
        replies.append(content)
        return None

    monkeypatch.setattr(b, "safe_reply", _fake_reply)
    monkeypatch.setattr(b, "_UNDO_STACK", type(b._UNDO_STACK)(maxlen=50))
    b._UNDO_STACK.append((original, backup))
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID + 1))

    asyncio.run(b.cmd_undo(message))          # 不得拋

    assert original.read_text(encoding="utf-8") == "keep me\n", (
        "原檔被動到了——解不開的備份**不可以**被寫回去。")
    assert replies and "備份" in replies[0], f"沒有回覆使用者：{replies!r}"
    assert "UnicodeDecodeError" not in replies[0], (
        "原始例外文字流到 Discord 了（Secrecy Layer 1）。")
    assert "undo read backup failed" in capsys.readouterr().err


def _safe_write_env(tmp_path, monkeypatch):
    """把 `.backup/` 與 undo stack 指到 tmp，回傳要寫的目標檔。"""
    monkeypatch.setattr(b, "BACKUP_DIR", tmp_path / ".backup")
    monkeypatch.setattr(b, "_UNDO_STACK", type(b._UNDO_STACK)(maxlen=50))
    return tmp_path / "todo_prompt.md"


def test_safe_write_refuses_rather_than_dropping_the_undo_point(
        tmp_path, monkeypatch, capsys):
    """讀不出原檔就**整個拒絕寫入**，不是「照樣寫、只印 stderr」。

    這是本專案反覆出現的那條判準的直接應用：「宣稱做到了卻沒做到」比「明說做
    不到」糟得多。照樣寫的代價是默默取消這一次的 undo——而 undo 備份正是使用者
    不會當場檢查、只在出事時才發現不見的東西。

    順帶釘住「拒絕」不是嘴上說說：原檔的位元組必須一個都沒動，而且不可以留下
    一個半吊子的 .bak（那會讓 `/sys undo` 之後把亂碼寫回佇列）。
    """
    path = _safe_write_env(tmp_path, monkeypatch)
    path.write_bytes(UNDECODABLE)

    with pytest.raises(b._UndoBackupUnavailable) as caught:
        b._safe_write(path, "新內容\n")

    assert path.read_bytes() == UNDECODABLE, "拒絕寫入卻還是動了原檔。"
    assert caught.value.path == path
    assert isinstance(caught.value.cause, UnicodeDecodeError)
    assert not list((tmp_path / ".backup").glob("*.bak")), (
        "留下了備份檔——但那份備份的內容並不是原檔，`/sys undo` 會把它寫回去。")
    assert not b._UNDO_STACK, "拒絕的寫入不該在 undo stack 留下條目。"
    assert "prior read failed" in capsys.readouterr().err


def test_safe_write_also_refuses_when_the_backup_cannot_be_written(
        tmp_path, monkeypatch):
    """備份**寫**不出去跟讀不出來是同一個不變式：先有復原點才動原檔。

    兩邊走同一條拒絕路徑，否則這一半會退回成泛用的「內部錯誤」，使用者分不出
    他的編輯到底有沒有生效。這裡用一個「目錄佔住了 .bak 的名字」來製造 OSError，
    不必動權限（在 Windows 上不可靠）。
    """
    path = _safe_write_env(tmp_path, monkeypatch)
    path.write_text("原本的內容\n", encoding="utf-8")
    backup_dir = tmp_path / ".backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(b, "_backup_path_for", lambda _p: backup_dir)  # 是目錄

    with pytest.raises(b._UndoBackupUnavailable) as caught:
        b._safe_write(path, "新內容\n")

    assert path.read_text(encoding="utf-8") == "原本的內容\n", (
        "備份寫不出去卻照樣改了原檔——這次編輯無法復原。")
    assert isinstance(caught.value.cause, OSError)


def test_safe_write_still_works_when_the_prior_content_is_readable(
        tmp_path, monkeypatch):
    """守門的自我檢查：拒絕路徑不能把正常的寫入一起擋掉。

    少了這一支，把 `_safe_write` 改成「一律拋」也會讓上面兩支變綠。
    """
    path = _safe_write_env(tmp_path, monkeypatch)
    path.write_text("原本的內容\n", encoding="utf-8")
    b._safe_write(path, "新內容\n")
    assert path.read_text(encoding="utf-8") == "新內容\n"
    backups = list((tmp_path / ".backup").glob("todo_prompt.md.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "原本的內容\n"
    assert len(b._UNDO_STACK) == 1


def test_the_refusal_message_says_what_happened_without_leaking(
        tmp_path, monkeypatch):
    """Secrecy Layer 1 ＋ 「據實以告」兩件事一起釘。

    非擁有者只能看到泛用標籤：不得有真實檔名、主機路徑、原始例外文字。但訊息
    仍然要講清楚**這次沒有寫進去**——泛用不等於沒有資訊，那正是原本那句「指令
    發生內部錯誤」的問題。
    """
    path = tmp_path / "todo_prompt.md"
    error = b._UndoBackupUnavailable(
        path, UnicodeDecodeError("utf-8", b"\xb3", 0, 1, "invalid start byte"))

    stranger = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID + 1))
    text = b._backup_unreadable_reply(stranger, error)
    assert "主提示詞佇列" in text, "沒告訴使用者是哪一份清單。"
    assert "沒有寫入" in text, "沒說清楚這次的編輯沒有生效。"
    for banned in ("todo_prompt.md", str(tmp_path), "UnicodeDecodeError",
                   "invalid start byte"):
        assert banned not in text, f"洩漏了 {banned!r}（Secrecy Layer 1）。"

    owner = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    owner_text = b._backup_unreadable_reply(owner, error)
    assert "todo_prompt.md" in owner_text, (
        "擁有者應該拿得到真實檔名（2026-08-27 裁決，走 `_list_label` 的單一決策點）。")
    assert "UnicodeDecodeError" in owner_text, (
        "擁有者應該拿得到原始例外（走 `_owner_error`）。")



# ---------------------------------------------------------------------------
# bot 這一側的佇列／提示詞讀取：解不開要**拋**，不可以回空值
# ---------------------------------------------------------------------------
# 判準跟 `_webrunner_shared.QueueDecodeError` 完全一樣（理由見下面那段區塊註解），
# 只是換到 bot 這一側：`read_todo_entries` 回 `[]`、`read_file_text` 回 `""` 在下游
# 都是**合法且會改變行為的值**，所以「解不開」不可以長得跟「空的」一樣。

_BIG5_LINE = "主提示詞\n".encode("big5")     # cp950 的中文，UTF-8 解不開


def test_read_todo_entries_refuses_instead_of_reporting_an_empty_queue(tmp_path):
    """Big5 的佇列檔 → 拋 `_QueueFileNotUtf8`，**不是**回 `[]`。

    回 `[]` 的下場是安靜的錯誤結果：空佇列在下游會被換成 fallback 檔，而 fallback
    模式不 pop、跑完還回 rc=0。使用者看到「跑完了」，拿到的是別份內容產的圖。
    """
    path = tmp_path / "todo_prompt.md"
    path.write_bytes(_BIG5_LINE)
    with pytest.raises(b._QueueFileNotUtf8) as caught:
        b.read_todo_entries(path)
    error = caught.value
    assert error.path == path, "沒帶出是哪一個檔，派發層就講不出來。"
    assert isinstance(error.start, int) and error.reason, (
        "位元組位置與原因是診斷的全部——少了它們，這個型別只比裸的例外多一個名字。")
    assert isinstance(error.__cause__, UnicodeDecodeError), (
        "`from error` 的鏈結不見了：佇列內容不是憑證，保留 cause 換到完整 traceback "
        "是刻意的取捨（憑證那一側才用 `from None`）。")


def test_the_queue_decode_log_line_does_not_dump_the_whole_file(
        tmp_path, capsys):
    """那一行 stderr 要**逐欄印**，不可以是 `{error!r}`。

    `UnicodeDecodeError` 的 repr 會把 `object` 一起倒出來——那是整份檔案的原始
    位元組。而 stderr 進 `discord_bot.log`，`/log tail` 正是那個 log 的出口，所以
    「一個解不開的佇列檔」會變成「把那個檔案的全部內容送到聊天平台」。

    這一支是變異逼出來的：換成 `{error!r}` 之前**活了下來**，因為那個性質只寫在
    旁邊的註解裡。同一個檔案裡 `_QueueFileNotUtf8` 的 docstring 也宣稱「刻意不留
    `error.object`」——宣稱需要一個會咬的測試，否則它只是一句話。
    """
    sentinel = "SECRET-QUEUE-LINE-DO-NOT-LOG"
    path = tmp_path / "todo_prompt.md"
    path.write_bytes(sentinel.encode("utf-8") + _BIG5_LINE)

    with pytest.raises(b._QueueFileNotUtf8):
        b.read_todo_entries(path)

    err = capsys.readouterr().err
    assert err.strip(), "一行 log 都沒有——出事時的診斷全靠它"
    assert str(path) in err, (
        "完整路徑應該**留在 log 裡**（那是給主機上的人看的，也是唯一一次出現）。")
    assert sentinel not in err, (
        f"檔案內容被倒進 log 了，而 `/log tail` 會把 log 送到聊天平台：{err!r}")
    for dumped in ("object=", "\\xb3", "b'"):
        assert dumped not in err, (
            f"log 裡出現 `{dumped}`——看起來是印了整個例外的 repr："
            f"{err!r}")


def test_read_file_text_refuses_instead_of_reporting_an_empty_default(tmp_path):
    """同樣的判準用在提示詞 fallback／範本上。

    `""` 在下游的意思是「使用者就是要空的提示詞」，跟「這個檔讀不出來」必須分得開。
    """
    path = tmp_path / "prompt.md"
    path.write_bytes(_BIG5_LINE)
    with pytest.raises(b._QueueFileNotUtf8):
        b.read_file_text(path)


def test_the_two_readers_still_answer_normally(tmp_path):
    """守門的自我檢查：拒絕路徑不能把正常讀取一起擋掉。

    少了這一支，把兩支讀取端改成「一律拋」也會讓上面兩支變綠；而「檔案不存在 ＝
    空」是這兩支**正確**的空值，也要一起釘住，否則會有人連它一起改成拋。
    """
    good = tmp_path / "todo_prompt.md"
    good.write_text("第一筆\n第二筆\n", encoding="utf-8")
    assert b.read_todo_entries(good) == ["第一筆", "第二筆"]

    prompt = tmp_path / "prompt.md"
    prompt.write_text("內容\n", encoding="utf-8")
    assert b.read_file_text(prompt) == "內容\n"

    assert b.read_todo_entries(tmp_path / "missing.md") == []
    assert b.read_file_text(tmp_path / "missing.md") == ""


def test_the_not_utf8_exception_keeps_its_message_generic(tmp_path):
    """`str(exc)` 一個字都不從輸入來——這是 docstring 上那句宣告的機制檢查。

    它不在 `_SAFE_EXCEPTION_TYPES` 裡（理由寫在類別的 docstring），所以沒有那支
    AST 守門在看；但 `_handle_mention` 會把沒接住的例外原樣往外拋，讓 discord.py
    印整串 traceback 進 log，而 `/log tail` 會把那個檔案送進對話平台。訊息裡只要
    夾了路徑或那串解不開的位元組，就會一路流過去。
    """
    path = tmp_path / "todo_prompt.md"
    error = b._QueueFileNotUtf8(
        path, UnicodeDecodeError("utf-8", b"\xb3\x44", 0, 1, "invalid start byte"))
    text = str(error)
    assert text == b._QueueFileNotUtf8.GENERIC
    for banned in ("todo_prompt.md", str(tmp_path), "invalid start byte", "0xb3"):
        assert banned not in text, f"訊息裡夾了 {banned!r}。"


def test_the_not_utf8_reply_says_what_happened_without_leaking(tmp_path):
    """Secrecy Layer 1 ＋「據實以告」一起釘，形狀比照上面那支拒絕寫入的。

    非擁有者只拿得到泛用標籤，但訊息仍然要講清楚**是哪一份清單**跟**怎麼修**——
    泛用不等於沒有資訊，那正是原本那句「指令發生內部錯誤」的問題。
    """
    path = tmp_path / "todo_character1.md"
    error = b._QueueFileNotUtf8(
        path, UnicodeDecodeError("utf-8", b"\xb3\x44", 0, 1, "invalid start byte"))

    stranger = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID + 1))
    text = b._queue_not_utf8_reply(stranger, error)
    assert "角色1 佇列" in text, "沒告訴使用者是哪一份清單。"
    assert "UTF-8" in text, "沒說清楚該怎麼修。"
    for banned in ("todo_character1.md", str(tmp_path), "UnicodeDecodeError",
                   "invalid start byte"):
        assert banned not in text, f"洩漏了 {banned!r}（Secrecy Layer 1）。"

    owner = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    owner_text = b._queue_not_utf8_reply(owner, error)
    assert "todo_character1.md" in owner_text, (
        "擁有者應該拿得到真實檔名（2026-08-27 裁決，走 `_list_label` 的單一決策點）。")
    assert "UnicodeDecodeError" in owner_text, (
        "擁有者應該拿得到原始例外（走 `_owner_error`）。")


# 推導不出來、但**刻意**要有分支的表面。目前只有 reaction 這一個：⭐/🗑️ 只碰
# favorites，靜態呼叫圖走不到那兩支讀取端。但「走不到」是**目前這批 handler** 的
# 性質，不是那個表面的性質——少了它，哪天某個 reaction handler 一路讀到佇列檔，
# 使用者按了星星會什麼訊息都收不到（那個表面的泛用 except 只印 stderr）。
_DELIBERATE_REFUSAL_SURFACES = {"on_raw_reaction_add"}


def test_every_dispatcher_that_can_reach_the_queue_readers_explains_the_refusal():
    """每個到得了那兩支讀取端的派發層都要有自己的分支，排在泛用 handler **之前**。

    要求集合是**推導 ∪ 列舉**，形狀抄
    `test_bot_helpers.test_every_surface_that_can_reach_safe_write_explains_the_refusal`：
    `@client.event` 那一半從靜態呼叫圖推出來（以後有人接一個新事件、而它輾轉讀到
    這兩支，這支會自動要求它處理），動態派發的兩個表面靜態看不見只能列舉。純列舉
    是 fail-open 的——這個 repo 已經為那個形狀付過好幾次學費。

    順序是承重的：接在泛用 `except Exception` 後面等於沒接，使用者拿到的還是那句
    沒有資訊的「指令發生內部錯誤（_QueueFileNotUtf8）」。用 AST 比**位置**而不是
    比原始碼文字——這幾個分支的註解本身就提到了泛用 handler，字串比對會命中註解
    而讓一個壞掉的順序通過。

    **可達性只算同一個 task 裡的呼叫**（2026-09-21）。`_schedule_coro(f())` /
    `create_task(f())` 裡的 `f` 在另一個 task 裡跑，呼叫端的 `except` 永遠接不到它
    的例外，所以那條邊不算；反過來，**被交出去跑的 task 本體**自己就是一個表面
    ——它到得了讀取端，就得自己接。原本的呼叫圖把交出去的協程也算成一條邊，於是
    `on_ready` 排出延後啟動的還原之後被要求「接住」一個它根本看不到的例外（能滿足
    那個要求的只有一段永遠不會執行的分支），而真正會收到例外的排程工作
    （`_scheduled_run_loop`，`/run in` 從一開始就是它）反而從來沒被要求過。
    """
    import ast     # noqa: PLC0415

    from test_bot_helpers import (      # noqa: PLC0415
        _DYNAMIC_DISPATCHERS, _bot_functions, _callers_reaching,
        _client_event_names, _task_bodies)

    funcs = _bot_functions()
    reaching = (
        _callers_reaching(funcs, "read_todo_entries", through_handoff=False)
        | _callers_reaching(funcs, "read_file_text", through_handoff=False))
    assert len(reaching) > 20, (
        f"呼叫圖只推出 {len(reaching)} 個呼叫端。抽取器一壞，下面那句「都有分支」"
        "就會空轉成綠的——空的選擇跟乾淨的結果在輸出上一模一樣。")
    derived = (_client_event_names(funcs) | _task_bodies(funcs)) & reaching
    assert "_scheduled_run_loop" in derived, (
        "推導那一半沒有推出 `_scheduled_run_loop`（它被 `create_task` 交出去跑，"
        "而到點時會讀佇列檔）。少了這個正面對照，「task 本體也算表面」那一半"
        "壞掉不會有任何症狀。")
    assert "on_message" in derived, (
        "推導那一半沒有推出 `on_message`（它是 `@client.event`，而 `!` 指令一路"
        "讀得到佇列檔）。少了這個正面對照，整支測試會退化成只驗下面那份手寫清單，"
        "而呼叫圖壞掉不會有任何症狀。")
    required = derived | _DYNAMIC_DISPATCHERS | _DELIBERATE_REFUSAL_SURFACES

    tree = ast.parse(
        Path(b.__file__).read_bytes().decode("utf-8"), filename="discord_bot.py")
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in required:
            continue
        for block in ast.walk(node):
            if not isinstance(block, ast.Try):
                continue
            names = [ast.unparse(h.type) if h.type else ""
                     for h in block.handlers]
            if "_QueueFileNotUtf8" not in names:
                continue
            seen.add(node.name)
            ours = names.index("_QueueFileNotUtf8")
            blanket = [i for i, n in enumerate(names) if n == "Exception"]
            assert not blanket or ours < blanket[0], (
                f"{node.name}：`except _QueueFileNotUtf8` 排在泛用 handler 後面，"
                "等於沒接。")
    missing = sorted(required - seen)
    assert not missing, (
        f"{missing} 走得到 `read_todo_entries` / `read_file_text`，卻沒有 "
        "`except _QueueFileNotUtf8`。佇列／提示詞檔解不開時，那個表面的使用者只會"
        "看到一句沒有資訊的泛用錯誤——而他真正需要知道的是「是哪一份清單、請重新"
        "存成 UTF-8」。")


# ---------------------------------------------------------------------------
# _webrunner_shared 的五處：佇列讀取端**刻意往外拋**，其餘三處吞
# ---------------------------------------------------------------------------
# 這一段跟上面所有測試方向相反，所以理由寫在這裡而不是散在各支裡。
#
# 前面每一個載入器讀的都是**設定**：讀不到就退回預設，而「預設」跟「讀到的值」
# 在下游分得開。佇列不是。「空」在 webrunner 下游是一個合法且會改變行為的值：
#
#   - `read_queues()` 看到某條佇列空掉，會替換成 fallback 檔。所以「讀不出來」
#     會安靜地變成「拿另一份內容產圖」，而且 fallback 模式不 pop，跑完回 rc=0。
#     使用者看到「跑完了」，拿到的是錯的圖——CLAUDE.md 反覆點名的那種**靜默錯誤
#     結果**，比崩潰難查得多。
#   - `todo_character2.md` 的空白列本身就是「這一配對不要 Character 2」；
#     `undesired.md` 空字串本身就是「不設負面提示詞」。下游沒有任何辦法把
#     「真的空」跟「解不開」分開。
#
# 而且沒有任何重試救得回來：要有人把檔案重存成 UTF-8。往外拋會在**開 Chrome 之
# 前**（`run_preflight` → `read_queues`）就死掉，行程一秒內結束，監督者的
# rapid-fail giveup 收手並通知——所以「拋」不會變成無限重生迴圈。

def _redirect_queues(monkeypatch, tmp_path):
    """把八個佇列 / fallback 常數全部指到 tmp。

    **八個都要**：漏掉任何一個，測試會安靜地讀到 repo root 的真檔，於是「這條
    佇列是空的」變成「這條佇列有東西」，斷言的意義整個換掉。
    """
    for const in ("TODO_PROMPT_FILE", "TODO_FILE_1", "TODO_FILE_2",
                  "TODO_UNDESIRED_FILE", "PROMPT_FILE",
                  "CHARACTER1_FALLBACK_FILE", "CHARACTER2_FALLBACK_FILE",
                  "UNDESIRED_FILE"):
        monkeypatch.setattr(ws, const, tmp_path / getattr(ws, const).name)


def _blank_queues(tmp_path, monkeypatch):
    """四條佇列都指到不存在的檔＝空佇列，讓 fallback 那一半成為唯一的輸入。"""
    for name in ("TODO_PROMPT_FILE", "TODO_FILE_1", "TODO_FILE_2",
                 "TODO_UNDESIRED_FILE"):
        monkeypatch.setattr(b, name, tmp_path / f"{name.lower()}.md")


def _plan_and_preview(monkeypatch, tmp_path, prompt_bytes):
    """同一份 `prompt.md` 分別餵給 `/gen plan` 與 `/gen preview`，回兩邊的結果。

    回的是 `{指令: ("ok", 回覆) 或 ("raised", 型別名)}`——**兩個指令、一份語料**，
    所以比得出「同一個檔案，一邊活得下來、一邊炸掉」。兩支各自綠不算對帳。
    """
    from test_bot_helpers import _run_reply      # noqa: PLC0415

    _blank_queues(tmp_path, monkeypatch)
    prompt = tmp_path / "prompt.md"
    prompt.write_bytes(prompt_bytes)
    monkeypatch.setattr(b, "PROMPT_FILE", prompt)
    for name in ("CHARACTER1_FILE", "CHARACTER2_FILE", "UNDESIRED_FILE"):
        monkeypatch.setattr(b, name, tmp_path / f"{name.lower()}.md")

    out = {}
    for label, factory in (
            ("plan", lambda: b.cmd_plan(object(), "")),
            ("preview", lambda: b.cmd_preview(object()))):
        try:
            out[label] = ("ok", _run_reply(monkeypatch, factory))
        except Exception as error:      # noqa: BLE001 — 就是要看它有沒有炸
            out[label] = ("raised", type(error).__name__)
    return out


def test_the_preview_survives_the_same_fallback_the_plan_survives(
        tmp_path, monkeypatch):
    """同一個 Big5 的 `prompt.md`：`/gen plan` 與 `/gen preview` 必須同生共死。

    這一筆在 `_UNTRIED_PENDING` 裡躺著，理由寫得很清楚：「`/gen preview` 自己
    重寫了一次 fallback 讀取，沒有走已經守好的 `_fallback_text`」。`cmd_preview`
    判斷「有沒有東西可預覽」時用的是守過的那支，**底下取內容時又自己寫了三次**，
    於是同一個檔案先通過守門、再在下一行炸掉。

    斷言寫成**兩邊比對**而不是各自斷言「不會炸」：兩支各自綠不等於兩支一致，而
    這個缺陷的本體正是「兩條路對同一份輸入給出不同答案」。
    """
    got = _plan_and_preview(monkeypatch, tmp_path, UNDECODABLE)
    assert got["plan"][0] == got["preview"][0], (
        f"同一個 Big5 的 `prompt.md`，兩個指令的下場不一樣：{got}")
    assert got["plan"][0] == "ok", (
        f"連 `/gen plan` 都炸了，那是另一個缺陷：{got}")


def test_the_plan_preview_parity_check_is_not_vacuous(tmp_path, monkeypatch):
    """反面對照：換成**讀得開**的 fallback，兩邊也要都活著而且真的有回覆。

    沒有這一格的話，上面那支在「兩邊都炸」時也會通過前半段的相等比對——雖然
    第二句斷言擋得住，但這一格讓「語料真的跑得動」變成獨立的證據。
    """
    got = _plan_and_preview(monkeypatch, tmp_path,
                            "a girl standing".encode("utf-8"))
    assert got["plan"][0] == "ok" and got["preview"][0] == "ok", got
    assert got["plan"][1] and got["preview"][1], f"有一邊沒有回覆：{got}"


def test_pushing_an_undecodable_default_does_not_call_it_empty(
        tmp_path, monkeypatch, capsys):
    """範本解不開時**不可以**回「是空的」——檔案其實是滿的，只是編碼不對。

    那句話會讓使用者去打開檔案，然後看到裡面明明有字。清單裡對這一筆的理由就是
    「回空字串會被下游當成『範本是空的』」。

    同時釘住 Secrecy Layer 1：回覆裡不得出現主機路徑，也不得出現原始例外文字。
    """
    from test_bot_helpers import _run_reply      # noqa: PLC0415

    source = tmp_path / "some_template.md"
    source.write_bytes(UNDECODABLE)
    dest = tmp_path / "todo_prompt.md"
    text = _run_reply(monkeypatch,
                      lambda: b._cmd_push_default(object(), source, dest))

    assert text, "什麼都沒回"
    assert "是空的" not in text, (
        f"把「解不開」報成「是空的」了：{text!r}。檔案其實是滿的。")
    assert str(source) not in text and source.name not in text, (
        f"回覆帶了主機路徑：{text!r}")
    assert "codec" not in text and "byte" not in text, (
        f"回覆帶了原始例外文字：{text!r}")
    assert not dest.exists() or not dest.read_bytes(), (
        "解不開的內容被寫進佇列了")


def test_the_token_reader_says_what_is_wrong_without_printing_the_token(
        tmp_path):
    """token 檔解不開 → 要停，而且要講得出原因；但**一個位元組都不能印出來**。

    方向（停下來）本來就是對的，缺的是訊息——這一筆在清單裡寫的是「可讀性缺口」。
    但它同時是外送邊界：`UnicodeDecodeError` 的訊息會把解不開的那個位元組值印出
    來，而這個檔案**整份就是一個 token**。所以這裡連例外鏈一起查（`from None`），
    與 `_webrunner_shared.CredentialsError` 同一個處置。
    """
    import traceback                            # noqa: PLC0415

    sentinel = "TOKEN-SENTINEL-DO-NOT-PRINT"
    path = tmp_path / "discord_bot_token.md"
    path.write_bytes(sentinel.encode("utf-8") + UNDECODABLE)

    try:
        b.read_token(path)
    except Exception as error:                  # noqa: BLE001
        # `as error` 綁的名字在 `except` 區塊結束時會被刪掉，所以型別要在這裡
        # 就抓下來，不能留到後面才 `isinstance`。
        kind = type(error)
        blob = "".join(traceback.format_exception(
            kind, error, error.__traceback__))
    else:
        pytest.fail("解不開的 token 檔居然讀得出東西")

    assert not issubclass(kind, UnicodeDecodeError), (
        f"還是裸的 `{kind.__name__}`——它的訊息會把位元組值印出來。")
    assert sentinel not in blob, f"token 內容跑進錯誤訊息了：\n{blob}"
    for leaked in ("0xb3", "0xB3", "UnicodeDecodeError"):
        assert leaked not in blob, (
            f"`{leaked}` 出現在 traceback 裡——例外鏈沒有用 `from None` 收掉，"
            f"而這個檔案整份就是一個 token。\n{blob}")
    assert "UTF-8" in blob or "utf-8" in blob, (
        f"訊息沒講出原因，跟修之前的裸 traceback 一樣難讀：\n{blob}")


def test_the_todo_queue_reader_refuses_to_pretend_the_queue_is_empty(tmp_path):
    """回 `[]` 會被下游當成「佇列消化完了」。那是**錯的答案**，不是保守的答案。"""
    path = tmp_path / "todo_prompt.md"
    path.write_bytes(UNDECODABLE)
    with pytest.raises(ws.QueueDecodeError) as caught:
        ws.read_todo_characters(path)
    assert "todo_prompt.md" in str(caught.value), (
        "訊息沒點名是哪個檔——裸的 UnicodeDecodeError traceback 最後一行是 "
        "`read_text`，看起來像「檔案讀不到」，那正是要修掉的誤導。")
    assert "UTF-8" in str(caught.value), "訊息沒告訴人該怎麼修。"


def test_the_positional_character2_queue_also_refuses(tmp_path):
    """`preserve_blank=True` 這條路更危險：空白列本身就是合法輸入。

    回 `[]` 在這裡不只是「沒東西」，是「每一個配對都不要 Character 2」——會安靜
    地把整批圖的角色拿掉。
    """
    path = tmp_path / "todo_character2.md"
    path.write_bytes(UNDECODABLE)
    with pytest.raises(ws.QueueDecodeError):
        ws.read_todo_characters(path, preserve_blank=True)


def test_the_fallback_reader_refuses_to_pretend_the_fallback_is_empty(tmp_path):
    """`read_text_safe` 的 "safe" 是指「不存在也沒關係」，不是「壞掉也沒關係」。

    它本來就只吞 `FileNotFoundError`（權限錯誤一直是往外拋的），所以把解碼失敗
    歸到「往外拋」那一邊才跟既有語意一致。`undesired.md` 是最好的例子：回空字串
    ＝ 這批圖**不設負面提示詞**，而那是一個合法設定，沒有人會發現讀錯了。
    """
    path = tmp_path / "undesired.md"
    path.write_bytes(UNDECODABLE)
    with pytest.raises(ws.QueueDecodeError):
        ws.read_text_safe(path)
    assert ws.read_text_safe(tmp_path / "does_not_exist.md") == "", (
        "「檔案不存在 → 空字串」是既有語意，不可以一起改掉。")


def test_read_queues_fails_loudly_instead_of_substituting_the_fallback(
        tmp_path, monkeypatch):
    """這支才是真正釘住那個決定的一支。

    場景：`todo_prompt.md` 壞編碼，而 `prompt.md` 好好的。回 `[]` 的版本會**成功
    回傳**、拿 fallback 的內容當提示詞、產一個角色的圖、回 rc=0。這裡斷言它必須
    拋——也就是「寧可停下來，不要拿錯的提示詞產圖」。
    """
    _redirect_queues(monkeypatch, tmp_path)
    ws.TODO_PROMPT_FILE.write_bytes(UNDECODABLE)
    ws.PROMPT_FILE.write_text("完全不同的提示詞\n", encoding="utf-8")
    ws.TODO_FILE_1.write_text("some character\n", encoding="utf-8")
    with pytest.raises(ws.QueueDecodeError):
        ws.read_queues()


def test_the_reconcile_backup_keeps_the_exact_bytes_from_disk(
        tmp_path, monkeypatch):
    """備份走 `read_bytes`/`write_bytes`，不解碼——所以它連解碼失敗的機會都沒有。

    原本是 `read_text` + `except OSError: raw = ""`。把 `UnicodeDecodeError` 加
    進那個元組**也是錯的**：那條路會寫出一個空備份檔，然後照樣印「原磁碟內容已
    備份到 X」——宣稱保住了、實際什麼都沒保住（本 repo 反覆出現的「log 要報結果
    不是報嘗試」）。改成位元組級保真同時修掉兩件事。

    這裡用**裸 `\\n`** 當換行：舊的 `write_text` 在 Windows 上會把它翻成
    `\\r\\n`，所以這個斷言分得出新舊。
    """
    monkeypatch.setattr(ws, "PROJECT_ROOT", tmp_path)
    path = tmp_path / "todo_prompt.md"
    raw = "第一筆\n第二筆\n".encode("utf-8")
    path.write_bytes(raw)
    # 記憶體裡的版本跟磁碟不同 → 觸發 divergence 分支。
    assert ws.reconcile_todo_with_disk(path, ["別的東西"]) == ["第一筆", "第二筆"]
    backups = list((tmp_path / ".backup").glob("todo_prompt.md.*.bak"))
    assert len(backups) == 1, f"備份沒寫出來：{backups!r}"
    assert backups[0].read_bytes() == raw, (
        "備份不是磁碟上的原始位元組。備份的職責就是保真——解一次碼再編回去"
        "既沒必要又會失真。")


def test_a_bad_dom_request_file_does_not_kill_the_batch(tmp_path, monkeypatch):
    """這一處的內容根本沒被用到（「dump 全部」是唯一模式），讓壞編碼把整輪批次
    炸掉毫無道理。刪檔要照樣發生，否則下一圈會再觸發一次。"""
    path = tmp_path / "dom_request.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(ws, "DOM_REQUEST_FILE", path)
    events: list = []
    monkeypatch.setattr(ws, "emit_event",
                        lambda name, **kw: events.append((name, kw)))

    class _Port:
        def execute_script(self, *_a, **_k):
            return []

    ws.check_dom_request(_Port())              # 不得拋
    assert not path.exists(), "請求檔沒被刪掉，下一圈會重複觸發。"
    assert [name for name, _ in events] == ["dom_result"]


def test_a_bad_single_image_request_fails_only_that_request(
        tmp_path, monkeypatch):
    """單圖請求壞掉只該讓**那個請求**失敗。

    這裡跟佇列讀取端刻意相反：req 留空 → serve 拿不到 prompt → 發一則
    `ok=false` 的 `single_image_done` → finally 刪檔。拋出去會把正在跑的整批
    角色一起帶走，代價完全不成比例。
    """
    path = tmp_path / "single_image_request.json"
    path.write_bytes(UNDECODABLE)
    monkeypatch.setattr(ws, "SINGLE_IMAGE_REQUEST_FILE", path)
    served: list = []
    monkeypatch.setattr(ws, "serve_single_image_request",
                        lambda port, req, in_band=True: served.append(req))

    assert ws.check_single_image_request(object()) is True
    assert served == [{}], f"serve 沒被呼叫到、或收到了亂碼：{served!r}"
    assert not path.exists(), "請求檔沒被刪掉，下一圈會重複觸發。"


# ---------------------------------------------------------------------------
# 解不開的變異標記——這一支的爆炸半徑是**整套測試跑不起來**
# ---------------------------------------------------------------------------
# `interrupted_targets()` 由 `conftest.pytest_configure` 在**收集之前**呼叫，所以
# 一個裸的 `UnicodeDecodeError` 不是「某一支測試紅了」，是整套連收集都進不去，而
# traceback 會指著 `read_text`，看起來像是磁碟壞了。
#
# 方向跟本檔上半部相反，而且是刻意的：設定檔那一族「解不開就退回預設」，這一支
# 「解不開就**算髒**」。差別在後果——退回預設的最壞情況是行為回到已知狀態，而這裡
# 退回「沒有髒檔」等於放行一棵可能還躺著變異的樹，那正是這支查詢存在的理由。
# 「查不出來」不可以長得跟「乾淨」一樣。

def test_an_undecodable_marker_counts_as_dirty(tmp_path):
    """標記讀不出來 → 算髒，而且不可以往外拋。"""
    import mutation_harness as mh

    (tmp_path / "whatever.inprogress").write_bytes(UNDECODABLE)
    dirty = mh.interrupted_targets(tmp_path)
    assert dirty, (
        "解不開的標記被當成乾淨了。讀不出目標檔名就無法比對內容，"
        "而『查不出來』當成『沒事』正是這支查詢要堵的洞。")
    assert any("whatever.inprogress" in row for row in dirty), (
        f"訊息沒有指出是哪一個標記檔，人看了不知道要去刪什麼：{dirty}")


def test_the_undecodable_marker_message_survives_the_conftest_formatting():
    """conftest 會把回傳值 `'、'.join(...)`，所以每一筆都必須是字串。

    回傳一個 `Path` 或 tuple 在這裡會炸，而炸的地點是 `pytest_configure`——
    一樣是整套跑不起來，只是換一個例外類別。
    """
    import mutation_harness as mh

    rows = mh.interrupted_targets(mh.DEFAULT_SNAPSHOT_DIR)
    assert all(isinstance(row, str) for row in rows), rows
    "、".join(rows)


def test_a_marker_deleted_mid_read_is_not_dirty(tmp_path, monkeypatch):
    """標記在 glob 與 read 之間被刪掉 → 那是**正常收工**的競態，不算髒。

    這一格分開測，是因為 `FileNotFoundError` 是 `OSError` 的子類別：把兩個
    `except` 的順序寫反，正常收工就會被誤報成髒，而那道訊息會擋掉整套測試。
    """
    import mutation_harness as mh

    marker = tmp_path / "gone.inprogress"
    marker.write_text("axiomatic/whatever.py", encoding="utf-8")

    real = Path.read_text

    def vanish(self, *args, **kwargs):
        if self.name == "gone.inprogress":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanish)
    assert mh.interrupted_targets(tmp_path) == [], (
        "正常收工的競態被誤報成髒了——這道誤報會擋掉整套測試的收集。")


# ---------------------------------------------------------------------------
# 名單本身要對得起來
# ---------------------------------------------------------------------------

_GUARDED = {
    ("_bot_config.py", "load_bot_config"),
    ("_batch_config.py", "load_batch_config"),
    ("_batch_config.py", "read_raw_batch_config"),
    ("_chrome_slot.py", "read_holder"),
    ("_run_progress.py", "read_progress"),
    ("discord_rpc.py", "load_rpc_config"),
    ("presence_probe.py", "_load_game_whitelist"),
    ("presence_probe.py", "_load_music_rules"),
    ("presence_probe.py", "_load_claude_detection"),
    # webrunner 共用層。前三個是「吞」，後兩個是**刻意往外拋**——理由見上面那段
    # 區塊註解（佇列的「空」在下游是合法值，退回空值 ＝ 靜默拿錯內容產圖）。
    ("_webrunner_shared.py", "reconcile_todo_with_disk"),
    ("_webrunner_shared.py", "check_dom_request"),
    ("_webrunner_shared.py", "check_single_image_request"),
    ("_webrunner_shared.py", "read_text_safe"),
    ("_webrunner_shared.py", "read_todo_characters"),
}

# **這份名單在 2026-09-07 清空了。** 它曾經列著「有人想接、但接錯型別」的檔案
# （`except (FileNotFoundError, OSError)` 接不住 `UnicodeDecodeError`，因為後者是
# `ValueError` 的子類）。最後一筆是 `verify_browser.py`——修它的時候順帶發現真正
# 的缺陷不只是型別：那支 `_live_webrunner_pid` 把「檔案不存在」與「檔案在但讀不
# 出來」都回成 `None`，而 `None` 在呼叫端的意思是「沒有正式作業，可以開瀏覽器」。
#
# 空集合是**刻意**的，不是還沒填。下面的 `test_no_read_text_site_is_left_unguarded`
# 因此變成零容忍：任何新的「在只接 OSError 的 try 裡、又沒帶 errors= 的
# `read_text`」都會立刻變紅。要再開豁免就要先在這裡寫清楚為什麼。
# （這裡原本指著 `test_the_remaining_sites_do_not_grow`，那是那支測試升級成零容忍
# 之前的舊名字；往下第 729 行那句「原本叫…」講的是同一次改名，那句是歷史、這句
# 是指路，兩者的正確寫法不一樣。）
_PENDING: set[str] = set()

# 第二份名單：`read_text` **完全不在任何 try 裡**的地方。
#
# 為什麼要分成兩份而不是併進 `_PENDING`：兩者的失敗形狀不同，該做的事也不同。
# 上面那一份是「有人想接、但接錯型別」（把 `UnicodeDecodeError` 加進 except 元組
# 就好）；這一份是「根本沒打算接」，所以每一處都得先**決定**解不開的時候該當作
# 什麼，而正確答案每處不一樣（見上面 `_webrunner_shared` 那一段區塊註解：設定檔
# 退回預設是對的，佇列退回空值是靜默的錯誤結果）。
#
# `_safe_write` 就是這個盲區的實例：它的 `prior = path.read_text(...)` 一直不在
# 任何 try 裡，所以只掃「在 try 裡」的定位器**永遠看不到它**——而它的後果是使用者
# 的佇列編輯以一句泛用「內部錯誤」告終。修好之後它自然從這份名單消失（現在那個
# 讀取被 `except (OSError, UnicodeDecodeError)` 包著，並轉成
# `_UndoBackupUnavailable` 這個「拒絕寫入」的明確訊息）。
#
# 鍵用 **(檔名, 函式名)** 而不是檔名：檔名太粗，`discord_bot.py` 一旦進了名單，
# 之後在同一個檔任何地方新加的讀取都會被順便遮掉；行號又會隨編輯漂移。函式名
# 兩邊都不犯。
#
# **這份名單在 2026-09-21 清空了。** 最後兩筆是 bot 這一側的佇列／提示詞讀取
# （`discord_bot.read_todo_entries` 與 `read_file_text`）。處置照的是
# `read_todo_characters` 那一段早就定好的判準——佇列讀取端不可以回空值——所以它
# 們改成丟 `discord_bot._QueueFileNotUtf8`：訊息是寫死的泛用句，檔名／位元組位
# 置／底層例外分別放在 `path` / `start`+`reason` / `cause` 屬性上，由
# `_queue_not_utf8_reply` 走既有的單一決策點（`_list_label` / `_owner_error`）
# 依提問者身分決定要露出多少。派發層那 4 個點（`_handle_mention` /
# `on_raw_reaction_add` / `on_message` / `_slash_run`）各加了一條排在 blanket
# `except Exception` 之前的分支，與 `_UndoBackupUnavailable` 同一個形狀。
#
# ⚠️ **空集合讓下面兩支測試變成一強一弱，這是刻意留著的現況，不是疏漏。**
# `test_the_untried_sites_do_not_grow` 從棘輪升級成零容忍（`seen - set()` ＝
# `seen`，任何新的無 try 讀取立刻變紅），而
# `test_the_untried_pending_list_has_no_stale_entries` 變成**恆真**——
# `set() - seen` 永遠是空的，沒有任何輸入能讓它失敗。它留著是為了「日後再開豁免
# 時反方向也有人盯」，不是因為它現在在守什麼。掃描器本身另有正面對照
# （`test_the_scan_finds_the_sites_it_is_meant_to_find` 那支合成 `probe.py`），
# 所以零容忍那一支不會因為掃描器壞掉而空轉。
#
# 空集合是**刻意**的，不是還沒填。要再開豁免，就在這裡寫清楚為什麼，不要把上面
# 那支零容忍測試放寬——「先關掉守門再說」才是這類清單真正的失效方式。
_UNTRIED_PENDING: set[tuple[str, str]] = set()

# 掃描不看的檔：由人在終端機手動執行的工具。那裡的 traceback 是**最好**的輸出
# ——執行的人當場看得到、也馬上改得動；包起來反而會把原因藏掉。名單保持極短，
# 任何長命行程（bot / 背景產圖 / 監督者）都不得列入。
_SCAN_SKIP_FILES = {"gen_command_docs.py", "audit_dependencies.py"}

_DECODE_COVERS = frozenset({
    "UnicodeDecodeError", "ValueError", "Exception", "BaseException"})


def _scan_read_text(name: str, tree) -> list:
    """回 [(檔名, 函式名, 行號, 有沒有被 try 包住, 那個 try 接不接得住)]。

    兩支定位器共用同一個走訪，**而且自我檢查也餵給這一支**——之前的自我檢查把
    走訪邏輯抄了一份在測試裡，所以它驗的是那份複本，真正的掃描器壞掉照樣綠。
    """
    import ast
    out = []

    def walk(node, chain, func):
        for child in ast.iter_child_nodes(node):
            here = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                here = child.name
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "read_text"
                    and not any(k.arg == "errors" for k in child.keywords)):
                covered = any(names & _DECODE_COVERS for names in chain)
                out.append((name, func, child.lineno, bool(chain), covered))
            if isinstance(child, ast.Try):
                names = set()
                for handler in child.handlers:
                    if handler.type is None:
                        names.add("Exception")
                    elif isinstance(handler.type, ast.Tuple):
                        names |= {ast.unparse(e) for e in handler.type.elts}
                    else:
                        names.add(ast.unparse(handler.type))
                for stmt in child.body:
                    walk(stmt, chain + [names], here)
                for stmt in child.orelse + child.finalbody:
                    walk(stmt, chain, here)
                for handler in child.handlers:
                    for stmt in handler.body:
                        walk(stmt, chain, here)
            else:
                walk(child, chain, here)

    walk(tree, [], "<module>")
    return out


def _scanned_sources(pkg_root=None, repo_root=None) -> tuple:
    """每一個非測試、非手動工具的模組——**含 repo root 的啟動器**。

    2026-09-10 之前這裡只 glob 套件目錄。而這條規則（長命行程不得因為一個解不開
    的位元組留下裸 traceback）最該管的就是那兩支**監督者**，它們住在 repo root。
    今天它們的三個 `read_text` 都在接得住的 try 裡，所以加寬掃不出任何東西——
    §8.8(A3)：正因為乾淨，範圍才需要自己的釘子（見
    `test_the_scan_is_a_glob_not_a_hardcoded_list`）。

    兩個目錄可以換掉，就是為了讓那根釘子測得出「這是算出來的，不是今天剛好對」。
    """
    package = (Path(__file__).resolve().parent.parent / "axiomatic" if pkg_root is None
               else pkg_root)
    root = package.parent if repo_root is None else repo_root
    out = []
    for path in sorted(package.glob("*.py")) + sorted(root.glob("*.py")):
        if path.name.startswith(("test_", "_test_")):
            continue
        if path.name in _SCAN_SKIP_FILES or path.name == "conftest.py":
            continue
        out.append(path)
    return tuple(out)


def _scan_project() -> list:
    import ast
    out = []
    for path in _scanned_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        out.extend(_scan_read_text(path.name, tree))
    return out


def _unguarded_read_text_sites() -> list:
    """`read_text` 沒帶 `errors=`、又在一個接不住 `UnicodeDecodeError` 的 try 裡。"""
    return [(name, func, lineno) for name, func, lineno, in_try, covered
            in _scan_project() if in_try and not covered]


def _untried_read_text_sites() -> list:
    """`read_text` 沒帶 `errors=`、而且**完全不在任何 try 裡**。

    這是上面那支的盲區：它只看得到「有 try 但接錯型別」，所以一個從頭到尾沒人
    想接的讀取對它是隱形的。
    """
    return [(name, func, lineno) for name, func, lineno, in_try, _covered
            in _scan_project() if not in_try]


def test_the_scan_finds_the_sites_it_is_meant_to_find():
    """守門的自我檢查：比對條件被改壞時，真實原始碼會照樣是空的。

    所以先拿一段合成原始碼證明它真的命中——每一支只踩一個條件，任何一條判斷被
    拿掉都會有一個斷言變紅（兩道防護互相遮蔽是這一族守門反覆出現的病）。**餵的
    是真正的 `_scan_read_text`**，不是抄一份在測試裡的複本。
    """
    import ast
    probe = ast.parse(
        "def a(p):\n"                                  # 在 try 裡、接不住
        "    try:\n"
        "        return p.read_text(encoding='utf-8')\n"
        "    except OSError:\n"
        "        return ''\n"
        "def b(p):\n"                                  # 帶了 errors= → 不算
        "    try:\n"
        "        return p.read_text(encoding='utf-8', errors='replace')\n"
        "    except OSError:\n"
        "        return ''\n"
        "def c(p):\n"                                  # 接得住 → 不算
        "    try:\n"
        "        return p.read_text(encoding='utf-8')\n"
        "    except (OSError, ValueError):\n"
        "        return ''\n"
        "def d(p):\n"                                  # 完全不在 try 裡
        "    return p.read_text(encoding='utf-8')\n"
        "def e(p):\n"                                  # try 的 except 區塊內 → 不算被包住
        "    try:\n"
        "        pass\n"
        "    except OSError:\n"
        "        return p.read_text(encoding='utf-8')\n")
    rows = _scan_read_text("probe.py", probe)
    in_try = sorted(lineno for _n, _f, lineno, tried, cov in rows
                    if tried and not cov)
    no_try = sorted(lineno for _n, _f, lineno, tried, _c in rows if not tried)
    assert in_try == [3], f"「在 try 裡但接不住」應該只命中 a()，實際 {in_try}"
    assert no_try == [17, 22], (
        f"「完全不在 try 裡」應該命中 d() 與 e() 的 except 區塊，實際 {no_try}")
    funcs = {func for _n, func, _l, _t, _c in rows}
    assert {"a", "c", "d", "e"} <= funcs, (
        f"函式名沒抓對——名單用 (檔名, 函式名) 當鍵，抓錯就等於名單失效：{funcs}")


def test_the_read_text_scan_has_a_corpus():
    """這一族守門全部站在 `_scan_project()` 上，所以它得先證明自己掃到了東西。

    ⚠️ **兩份名單清空之後，這一支就是唯一分得出「全部合規」與「掃描器壞了」的
    測試。** `test_the_untried_sites_do_not_grow` 的斷言會退化成
    `assert not seen`——語料是真的時候那是最強的形態，語料是空的時候它一樣綠。
    而掃描器自己的合成對照組證的是**判斷條件**對不對（餵的是一段寫死的原始碼），
    證不到「真的有檔案被走訪」。兩者合起來才完整。

    門檻取得比現值寬鬆，因為這裡要抓的是「整批掉下去」，不是日常增減：量到的是
    35 個來源檔、54 個呼叫點、21 個檔有命中（2026-09-21）。
    """
    sources = list(_scanned_sources())
    assert len(sources) >= 25, (
        f"只收集到 {len(sources)} 個**來源檔**——走訪整批掉下去了"
        "（glob 改了？模組搬家了？），下面那兩支會綠得跟「全部合規」一模一樣。")
    rows = _scan_project()
    assert len(rows) >= 40, (
        f"只掃到 {len(rows)} 個 `read_text` **呼叫點**——走訪到了檔案，但"
        "比對條件可能壞了。")
    assert len({name for name, *_rest in rows}) >= 15, (
        f"只有 {len({name for name, *_rest in rows})} 個**檔案有命中**"
        "——走訪可能只剩一小撮檔案。")


@pytest.mark.parametrize("victim,phrase", [
    ("_scanned_sources", "來源檔"),
    ("_scan_project", "呼叫點"),
])
def test_each_corpus_floor_fires_on_its_own_broken_premise(
        monkeypatch, victim, phrase):
    """**每一道下限要有自己的字眼，也要有自己的對照組。**

    第一版三句斷言共用「掃描語料」四個字，而對照組只比對那四個字——於是放寬其中
    一道、另一道照樣開火、訊息照樣命中，對照組分不出是哪一道在叫。變異實測：兩道
    下限各自放寬成 `>= 0` **都活了下來**。
    """
    monkeypatch.setattr(sys.modules[__name__], victim, lambda: [])
    with pytest.raises(AssertionError, match=phrase):
        test_the_read_text_scan_has_a_corpus()


def test_an_unguarded_read_text_anywhere_makes_the_guard_go_red(
        tmp_path, monkeypatch):
    """**範圍**那一半：注入一個沒登記的違規，守門必須紅。

    兩份缺口清單都清空之後，`test_the_untried_sites_do_not_grow` 的斷言退化成
    `assert not seen`——而真實的 `seen` 本來就是空集合，所以「把它改成不看真實掃描」
    在今天的資料上是**等價變異**，刪掉守門的邏輯不會有任何症狀。刪掉守門測的是
    邏輯，注入一個違規測的才是範圍，而這一族守門要的正是後者。
    """
    planted = tmp_path / "planted_module.py"
    planted.write_text(
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def reads_without_a_try(p: Path) -> str:\n"
        '    return p.read_text(encoding="utf-8")\n',
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_scanned_sources",
                        lambda: [planted])

    with pytest.raises(AssertionError, match="reads_without_a_try"):
        test_the_untried_sites_do_not_grow()


def test_the_untried_sites_do_not_grow():
    """完全沒被 try 包住的 `read_text` 是一份**會縮短**的名單，不是永久豁免。

    新出現在名單外的 (檔名, 函式名) 表示有人又寫了一個「解不開就整支炸掉」的
    讀取，卻沒先決定那裡到底該當作什麼。決定完就把它從 `_UNTRIED_PENDING` 移掉
    並補一支行為測試。
    """
    seen = {(name, func) for name, func, _lineno in _untried_read_text_sites()}
    unexpected = sorted(seen - _UNTRIED_PENDING)
    assert not unexpected, (
        f"{unexpected} 的 `read_text` 沒帶 `errors=`，而且完全不在任何 try 裡——"
        "解不開就會是一個裸的 `UnicodeDecodeError` 往上拋，使用者只會收到一句"
        "泛用的內部錯誤。請先決定「解不開的時候該當作什麼」（設定檔可以退回預設；"
        "佇列**不可以**退回空值，見本檔中段的區塊註解），然後在 "
        "`test_undecodable_files.py` 補一支行為測試。")


def test_the_untried_pending_list_has_no_stale_entries():
    """一個豁免必須有人證明它還在做它被豁免去做的事，否則它會活得比理由久。

    這一支盯的是反方向：名單裡列著、但實際上已經修好（或函式改名／被刪）的條目。
    留著它會讓下一個人以為那裡還有問題，也會讓名單再也不可能真的清空。
    """
    seen = {(name, func) for name, func, _lineno in _untried_read_text_sites()}
    ghosts = sorted(_UNTRIED_PENDING - seen)
    assert not ghosts, (
        f"{ghosts} 已經不再是「不在 try 裡的 read_text」了（修好了？改名了？"
        "被刪了？）。請把它從 `_UNTRIED_PENDING` 刪掉——名單的價值來自它是準的。")


def test_the_skip_list_only_holds_human_run_tools():
    """豁免整個檔案是最粗的一種豁免，所以它必須**小而且說得出理由**。

    判準是「執行的人當場看得到 traceback 嗎」：手動跑的產生器 / 稽核工具是，
    長命行程（bot、背景產圖、監督者、啟動器）不是。這裡只釘住它不會偷偷長大。
    """
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    for name in _SCAN_SKIP_FILES:
        assert (package / name).exists(), (
            f"{name} 不在了，請把它從 `_SCAN_SKIP_FILES` 刪掉。")
    assert _SCAN_SKIP_FILES == {"gen_command_docs.py", "audit_dependencies.py"}, (
        "有人替 `_SCAN_SKIP_FILES` 加了檔案。整個檔案不掃是最粗的豁免——只有"
        "「由人手動在終端機執行、traceback 就是最好的輸出」的工具適用。長命行程"
        "一律不得列入，請改成在 `_UNTRIED_PENDING` 逐點登記。")


def test_no_read_text_site_is_left_unguarded():
    """零容忍：不得有任何「在只接 `OSError` 的 try 裡、又沒帶 `errors=`」的
    `read_text`。

    這支原本叫 `test_the_remaining_sites_do_not_grow`，配著一份會縮短的
    `_PENDING` 名單。2026-09-07 名單清空，於是它從「不准變長」升級成「一個都不准
    有」——這正是當初那支棘輪測試（名單一空就變紅）要達成的目的。

    `_PENDING` 這個機制**刻意留著但保持空集合**：日後若真的需要暫時豁免，作法是
    在那裡登記並寫清楚理由，而不是把這支測試放寬。**「先關掉守門再說」是這類清單
    真正的失效方式**，留一個有紀律的出口比留一個沒有出口好。
    """
    files = {name for name, _func, _lineno in _unguarded_read_text_sites()}
    unexpected = files - _PENDING
    assert not unexpected, (
        f"{sorted(unexpected)} 有 `read_text` 沒帶 `errors=`、又在一個只接 "
        "`OSError` 的 try 裡。`UnicodeDecodeError` 是 `ValueError` 的子類別，"
        "接不住——把 `UnicodeDecodeError` 加進那個 except 元組，並在 "
        "`test_undecodable_files.py` 補一支行為測試。")




# ---------------------------------------------------------------------------
# 範圍與豁免前提，各自的釘子（§8.8）
# ---------------------------------------------------------------------------

_SCAN_FLOOR = 25


def _assert_scan_floor(sources) -> None:
    assert len(sources) >= _SCAN_FLOOR, (
        f"掃描集合只剩 {len(sources)} 個模組（下限 {_SCAN_FLOOR}）——列舉器壞了，"
        "或範圍被縮回去了。掃到 0 個時上面每一支都會過。")


def test_the_scan_covers_the_whole_project():
    _assert_scan_floor(_scanned_sources())


def test_the_floor_fires_when_the_enumerator_comes_back_empty():
    with pytest.raises(AssertionError, match="掃描集合只剩"):
        _assert_scan_floor(())


def test_the_scan_is_a_glob_not_a_hardcoded_list(tmp_path):
    """釘住「repo root 那一半是算出來的」。

    今天 repo root 的三個 `read_text` 全都在接得住的 try 裡，所以加寬與不加寬在
    真實資料上分不出來。唯一分辨得出來的辦法是餵它一個**清單裡不可能有的名字**。
    """
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "some_module.py").write_text("X = 1\n", encoding="utf-8")
    (root / "brand_new_supervisor.py").write_text("X = 1\n", encoding="utf-8")
    (root / "test_not_a_product_module.py").write_text("", encoding="utf-8")
    (root / "gen_command_docs.py").write_text("X = 1\n", encoding="utf-8")

    names = {p.name for p in _scanned_sources(pkg_root=pkg, repo_root=root)}
    assert "brand_new_supervisor.py" in names, (
        "repo root 新出現的腳本沒有被算進來——那一半是寫死的清單，不是 glob。"
        "而 repo root 住的正是兩支**監督者**，長命行程正是這條規則最該管的。")
    assert "some_module.py" in names
    assert "test_not_a_product_module.py" not in names, "測試檔不該進掃描"
    assert "gen_command_docs.py" not in names, "豁免名單在 repo root 那一半失效了"


def _tool_importers(sources) -> dict[str, set[str]]:
    """哪些**產品端**模組 import 了豁免名單裡的工具。"""
    import ast

    wanted = {name[:-3] for name in _SCAN_SKIP_FILES}
    found: dict[str, set[str]] = {}
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hits = {a.name for a in node.names} & wanted
            elif isinstance(node, ast.ImportFrom):
                hits = {node.module} & wanted if node.module else set()
            else:
                continue
            for hit in hits:
                found.setdefault(hit, set()).add(path.name)
    return found


def test_the_skip_lists_premise_is_actually_checked():
    """豁免的理由是「只有人在終端機手動跑」——那句話本身要有人驗。

    §8.8(B)：看到排除清單就問一句，它宣稱的前提有沒有人在檢查。這裡的前提是
    「沒有任何長命行程會執行到這兩個檔案」。前一版的 docstring 老實寫著「這裡
    只釘住它不會偷偷長大」——也就是承認前提沒被驗過。而前提一旦破掉，後果不是
    抽象的：那個檔案裡的裸 traceback 會在一個沒人看的主控台裡把長命行程弄死，
    而它**自動**豁免於這道守門。
    """
    importers = _tool_importers(_scanned_sources())
    assert not importers, (
        "這些被整檔豁免的手動工具已經被產品端模組 import 了："
        + str({k: sorted(v) for k, v in importers.items()})
        + "。豁免的前提（只有人在終端機手動跑）不成立了——"
        "把它從 `_SCAN_SKIP_FILES` 拿掉，改成逐點登記。")


def test_the_premise_check_actually_sees_an_importer(tmp_path):
    """控制組：真的種一個 importer 進去，上面那支必須指名它。

    真實資料是乾淨的，所以「有在看」和「永遠回空」在輸出上一模一樣。
    """
    fake = tmp_path / "long_lived_thing.py"
    fake.write_text("import gen_command_docs\n", encoding="utf-8")
    got = _tool_importers((fake,))
    assert got == {"gen_command_docs": {"long_lived_thing.py"}}, got

    from_form = tmp_path / "another_thing.py"
    from_form.write_text("from audit_dependencies import main\n",
                         encoding="utf-8")
    assert _tool_importers((from_form,)) == {
        "audit_dependencies": {"another_thing.py"}}, "`from X import` 那條路漏了"

    innocent = tmp_path / "innocent.py"
    innocent.write_text("import json\nimport audit_dependencies_helper\n",
                        encoding="utf-8")
    assert not _tool_importers((innocent,)), "名字只是前綴相同就被誤抓了"
