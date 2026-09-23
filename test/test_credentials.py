"""讀取明文憑證檔的那八行，以前一行都沒有被執行過。

`_webrunner_shared.read_credentials()` 是這個專案裡**唯一**讀 `auth.md` 的函式。
2026-09-21 量到它八行裡有七行從來沒有被任何測試跑過——而它同時是：

* 兩支 webrunner 啟動路徑上的第一件事（`webrunner_novelai.py` / `webrunner_je_only.py`
  都是 `email, password = read_credentials(AUTH_FILE)`，**外面沒有 try**）；
* 唯一一個手上同時握著使用者名稱與密碼的純函式。

所以它的每一條失敗路徑都同時是一個**啟動失敗**與一個**可能的外洩點**，而兩者都
沒有測試。這一份補的就是那些路徑。

## 這裡守的那條不變式

**這支函式的任何失敗，訊息裡都不得出現憑證值。** 它是唯一讀得到那些值的地方，所以
也是唯一能把它們塞進例外訊息的地方——而那些訊息會落進 `WEBRunner.log`，
`verify_browser` 的設定驗證更會把 `{err!r}` 直接印到主控台。

⚠️ 裸的 `UnicodeDecodeError` **會帶上出問題的那個位元組的值**
（`can't decode byte 0xff in position 12`）。同一個檔案裡 `_queue_decode_error()`
刻意只帶位置與 `error.reason`、不帶位元組值——憑證這一側現在是同樣的處置。

## 為什麼是型別化例外，而不是讓它自然炸

理由與 `QueueDecodeError` 同源，那個類別就在這支函式下面十行，docstring 寫得很
清楚：裸的 traceback 最後一行是 `read_text`，**看起來像「檔案讀不到」**，實際上
檔案好好的在那裡、只是編碼不對。憑證檔用一模一樣的 `read_text(encoding="utf-8")`
讀，本來卻沒有那個待遇——這一份的第一批測試就是在釘那個對齊。

`FileNotFoundError` 與其他 `OSError` **刻意不包**：那是「檔案不在／權限」，跟
「檔案在但內容不對」是兩件事，而 `webrunner_novelai._run_setup_verification` 的
註解也是照這個分類寫的。
"""
from __future__ import annotations

import os
import sys
import traceback

from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _webrunner_shared as ws  # noqa: E402

# 這些字串刻意長得不像任何合法的錯誤訊息片段，所以「訊息裡有沒有它」問得很乾脆。
_SENTINEL_USER = "sentinel-user@example.invalid"
_SENTINEL_PASSWORD = "S3NTINEL-PASSWORD-DO-NOT-PRINT"


def _auth(tmp_path: Path, body: str | bytes, name: str = "auth.md") -> Path:
    """在 tmp 造一個憑證檔。**永遠不碰 repo 裡那一份真的。**"""
    path = tmp_path / name
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 一、解析語意
# ---------------------------------------------------------------------------
def test_a_well_formed_file_yields_the_pair(tmp_path):
    path = _auth(tmp_path,
                 f"username: {_SENTINEL_USER}\npassword: {_SENTINEL_PASSWORD}\n")
    assert ws.read_credentials(path) == (_SENTINEL_USER, _SENTINEL_PASSWORD)


def test_keys_are_case_and_whitespace_insensitive(tmp_path):
    """欄位名容錯是刻意的：這個檔是人手寫的。"""
    path = _auth(tmp_path,
                 f"  UserName :{_SENTINEL_USER}\n\tPASSWORD:{_SENTINEL_PASSWORD}\n")
    assert ws.read_credentials(path) == (_SENTINEL_USER, _SENTINEL_PASSWORD)


def test_a_value_containing_a_colon_survives_whole(tmp_path):
    """密碼裡有冒號不算壞掉——`partition` 只切第一個。

    改成 `split(":")` 再取 `[1]` 會把密碼從冒號那裡剪斷，而症狀只是「登入失敗」，
    看不出原因。
    """
    secret = "a:b::c:"
    path = _auth(tmp_path, f"username: u\npassword: {secret}\n")
    assert ws.read_credentials(path) == ("u", secret)


def test_surrounding_whitespace_is_stripped_from_the_value(tmp_path):
    """⚠️ 這一格釘的是**現況**，而現況有一個安靜的失效模式。

    值會被 `strip()`，所以一個結尾帶空白的密碼會被悄悄改掉，而使用者看到的只有
    「登入失敗」。維持現況是對的（人手寫的檔案，前後空白幾乎都是手滑），但要有
    一支測試講出這件事，否則下次有人為了「修好那個登不進去的帳號」把 `strip()`
    拿掉時，會連欄位名後面那個對齊用的空格都一起帶進密碼。
    """
    path = _auth(tmp_path, "username:  u  \npassword:  pw with spaces  \n")
    assert ws.read_credentials(path) == ("u", "pw with spaces")


def test_lines_without_a_colon_are_ignored(tmp_path):
    path = _auth(tmp_path,
                 "# 這是註解\n\n隨手寫的一行\n"
                 f"username: u\npassword: {_SENTINEL_PASSWORD}\n")
    assert ws.read_credentials(path) == ("u", _SENTINEL_PASSWORD)


def test_a_repeated_key_takes_the_last_one(tmp_path):
    """重複欄位後者覆蓋前者——常見於「在舊的那行底下又貼了一行新的」。"""
    path = _auth(tmp_path,
                 "username: old\nusername: new\n"
                 f"password: {_SENTINEL_PASSWORD}\n")
    assert ws.read_credentials(path)[0] == "new"


# ---------------------------------------------------------------------------
# 二、失敗路徑要講得出「該做什麼」
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("missing,present", [("username", "password"),
                                             ("password", "username")])
def test_a_missing_field_is_reported_by_name(tmp_path, missing, present):
    """少一個欄位 → `CredentialsError`，而且訊息要指名是哪一個欄位少了。

    裸的 `KeyError('password')` 在 traceback 最後一行是一個 dict 查詢，讀的人得
    先去看原始碼才知道那是憑證檔的欄位名。
    """
    path = _auth(tmp_path, f"{present}: {_SENTINEL_PASSWORD}\n")
    with pytest.raises(ws.CredentialsError) as excinfo:
        ws.read_credentials(path)
    assert missing in str(excinfo.value), excinfo.value
    assert path.name in str(excinfo.value), excinfo.value


def test_a_field_line_with_no_colon_is_a_missing_field_not_an_empty_one(tmp_path):
    """半打完的一行（`password` 少了冒號）要報「少了欄位」，不是回一個空密碼。

    這一格是變異逼出來的：拿掉「沒有冒號的行略過」那道檢查之後，`partition` 會把
    整行當成欄位名、值是空字串——於是 `password` 這個鍵**存在但為空**，缺漏檢查
    就不會開火，函式安靜地回一組空密碼的憑證。症狀只有「登入失敗」，而那個檔案
    看起來明明就寫著 password。
    """
    path = _auth(tmp_path, "username: u\npassword\n")
    with pytest.raises(ws.CredentialsError) as excinfo:
        ws.read_credentials(path)
    assert "password" in str(excinfo.value), excinfo.value


def test_a_file_that_is_not_utf8_says_so_instead_of_looking_unreadable(tmp_path):
    """編碼不對 → `CredentialsError`，訊息要帶檔名與位元組位置。

    這是 `QueueDecodeError` 那一組的同款處置：裸的 traceback 最後一行是
    `read_text`，看起來像「檔案讀不到」，實際上檔案好好的在那裡。
    """
    body = ("username: u\npassword: " + _SENTINEL_PASSWORD + "\n").encode("utf-8")
    path = _auth(tmp_path, body[:20] + b"\xff\xfe" + body[20:])
    with pytest.raises(ws.CredentialsError) as excinfo:
        ws.read_credentials(path)
    message = str(excinfo.value)
    assert path.name in message, message
    assert "20" in message, f"訊息沒帶位元組位置：{message}"


def test_a_missing_file_still_raises_the_plain_os_error(tmp_path):
    """**刻意不包**：檔案不在是另一類問題，呼叫端的註解也是照這個分類寫的。"""
    with pytest.raises(FileNotFoundError):
        ws.read_credentials(tmp_path / "does_not_exist.md")


def test_the_typed_error_is_not_confusable_with_the_queue_one(tmp_path):
    """兩個型別化例外各管各的：接住其中一個不該連帶接住另一個。

    兩者都繼承 `RuntimeError`（呼叫端一律有泛用的後路），但誰也不是誰的子類。
    """
    assert issubclass(ws.CredentialsError, RuntimeError)
    assert issubclass(ws.QueueDecodeError, RuntimeError)
    assert not issubclass(ws.CredentialsError, ws.QueueDecodeError)
    assert not issubclass(ws.QueueDecodeError, ws.CredentialsError)


# ---------------------------------------------------------------------------
# 三、不變式：任何失敗都不得把憑證值講出來
# ---------------------------------------------------------------------------
def _broken_files(tmp_path: Path) -> list:
    """每一份都含著哨兵值，而且每一份都會讓 `read_credentials` 失敗。"""
    good = f"username: {_SENTINEL_USER}\npassword: {_SENTINEL_PASSWORD}\n"
    encoded = good.encode("utf-8")
    return [
        ("缺 username", _auth(tmp_path, f"password: {_SENTINEL_PASSWORD}\n", "a1.md")),
        ("缺 password", _auth(tmp_path, f"username: {_SENTINEL_USER}\n", "a2.md")),
        ("兩個欄位都缺（但檔案有內容）",
         _auth(tmp_path, f"note: {_SENTINEL_PASSWORD}\n", "a3.md")),
        ("非 UTF-8（壞位元組在哨兵前面）",
         _auth(tmp_path, encoded[:5] + b"\xff" + encoded[5:], "a4.md")),
        ("非 UTF-8（壞位元組在哨兵後面）",
         _auth(tmp_path, encoded + b"\xff\xfe\xfd", "a5.md")),
        ("UTF-16（整份都解不開）", _auth(tmp_path, good.encode("utf-16"), "a6.md")),
    ]


def test_no_failure_ever_names_a_credential_value(tmp_path):
    """這是這一份的核心不變式，跑在每一種壞法上。

    比對的是**格式化過的整串 traceback**，不只是 `str(error)`：例外鏈上還掛著
    什麼一樣會被印出來，只看訊息會漏掉那一半。

    ⚠️ 這一支同時是「有沒有真的失敗」的正面對照：每一份語料都必須真的丟例外，
    否則整個迴圈會一格都沒跑到而測試照樣綠——空的選擇看起來就像乾淨的結果。
    """
    checked = 0
    for label, path in _broken_files(tmp_path):
        try:
            ws.read_credentials(path)
        except Exception as error:      # noqa: BLE001 — 就是要看它講了什麼
            checked += 1
            blob = "".join(traceback.format_exception(
                type(error), error, error.__traceback__))
            for secret in (_SENTINEL_USER, _SENTINEL_PASSWORD):
                assert secret not in blob, (
                    f"{label}：憑證值出現在錯誤訊息裡了。"
                    f"\n{blob}")
        else:
            pytest.fail(f"{label}：這份語料居然讀得出憑證，它應該要失敗。")
    assert checked == 6, f"只跑到 {checked} 種壞法"


def test_the_offending_byte_value_never_reaches_the_traceback(tmp_path):
    """編碼那一種刻意用 `raise ... from None` 把例外鏈收掉——這一支釘的就是它。

    上面那條哨兵不變式抓不到這件事：裸的 `UnicodeDecodeError` 訊息帶的是**位元組
    值**（`can't decode byte 0xff in position 12`），不是密碼，所以哨兵不會出現、
    測試照樣綠。可是這個檔案**整份都是憑證**，而那個位元組正是檔案內容的一部分；
    訊息會進 log，log 送得到聊天平台。

    改成 `from error` 會讓那一行重新出現在 traceback 裡——這一支是那個決定唯一的
    守門。（與 `_queue_decode_error` 的 `from error` 刻意分歧：佇列檔的內容本來
    就不需要保密，保留鏈結換到的是更完整的診斷。）
    """
    body = ("username: u\npassword: " + _SENTINEL_PASSWORD + "\n").encode("utf-8")
    path = _auth(tmp_path, body[:20] + b"\xff" + body[20:])
    try:
        ws.read_credentials(path)
    except ws.CredentialsError as error:
        blob = "".join(traceback.format_exception(
            type(error), error, error.__traceback__))
    else:
        pytest.fail("這份語料應該要失敗")
    for leaked in ("0xff", "0xFF", "\\xff"):
        assert leaked not in blob, (
            f"出問題的位元組值 `{leaked}` 跑進 traceback 了——例外鏈沒有被收掉，"
            f"而那個位元組是憑證檔的內容。\n{blob}")
    assert "UnicodeDecodeError" not in blob, (
        "原始的 `UnicodeDecodeError` 還掛在例外鏈上（`from error`）。"
        f"\n{blob}")


def test_the_sentinel_check_would_actually_notice_a_leak(tmp_path):
    """反面對照：上面那支的比對方式真的抓得到洩漏。

    沒有這一支的話，`secret not in blob` 可能只是因為 `blob` 永遠是空的（或者
    哨兵字串被正規化掉了）而恆真——那種綠燈跟「沒有洩漏」長得一模一樣。
    """
    try:
        raise RuntimeError(f"密碼是 {_SENTINEL_PASSWORD}")
    except RuntimeError as error:
        blob = "".join(traceback.format_exception(
            type(error), error, error.__traceback__))
    assert _SENTINEL_PASSWORD in blob, (
        "比對方式看不到例外訊息裡的內容——上面那支不變式是空轉的。")


def test_the_broken_corpus_is_not_silently_empty(tmp_path):
    """語料下限。`_broken_files` 回空清單的話，上面那支會綠得跟「全部安全」一樣。"""
    files = _broken_files(tmp_path)
    assert len(files) >= 6, f"只有 {len(files)} 種壞法"
    labels = {label for label, _path in files}
    assert len(labels) == len(files), f"標籤重複，訊息會指不出是哪一格：{labels}"
