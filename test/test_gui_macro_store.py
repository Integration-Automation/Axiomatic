"""磁碟上的巨集檔可以被手改壞——`load_macro` 這樣寫，但它只驗了其中一個欄位。

`_gui_control.load_macro` 的 docstring 是這樣寫的：

    讀巨集並**重新驗證**每一步——檔案在磁碟上是可以被手動改壞的。

「每一步」是真的：`steps` 逐步重驗、區塊平衡也重算。但 `save_macro` 寫進檔案的是
**五個**欄位，而其中 `author_id` 會被 `edit_macro` 拿去 `int(...)`——沒有任何人驗過
它。實測九種值，五種讓一個**不是 `GuiError`** 的例外從 `edit_macro` 逃出去：

| 檔案裡的值 | 逃出來的 |
|---|---|
| `"abc"` | `ValueError` |
| `[1, 2]` / `{"a": 1}` | `TypeError` |
| `Infinity` / `1e400` | `OverflowError` |
| `NaN` | `ValueError` |
| `true` | （沒有例外，但 author_id 變成 **1**） |

`/macro edit` 那一側只 `except _GuiError`，所以這些會一路往上；而使用者看到的是
一句泛用失敗，且**再也沒辦法用 bot 把這個巨集改回來**——唯一的修法是回到那台
機器上手動編檔，而這整組指令存在的理由正是「下指令的人不在電腦前面」。

`1e400` 那一列值得單獨看：它**不需要有人手改檔案**，只要是個夠大的數字，
`json.loads` 就把它變成 `inf`（本專案已經為同一個家族寫過一整批數值守門）。
`true` 則是 `bool` 是 `int` 子類別的第四個實例——它不會炸，只會安靜地把作者記成
使用者 1。

所以這裡守的不是「那五個值」，是**規則**：`save_macro` 寫出去的每一個欄位，被改成
任何 JSON 值之後，巨集存取的入口只能丟 `GuiError`。欄位清單**從寫入端的字面值推
出來**，所以之後加第六個欄位會自動被納入。

順帶把同一個模組裡其他「宣告了上限、但拒絕那一邊從來沒被走過」的地方補齊
（分支覆蓋率 2026-09-21）：`load_macro` 的四條退路、`edit_macro` 的行號與步數
上限、`validate_macro_step` 的 `repeat` 次數、`write_host_file` 的位元組上限。
"""
import ast
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _gui_control as gui  # noqa: E402
from _gui_control import GuiError  # noqa: E402


# ---------------------------------------------------------------------------
# 欄位清單從寫入端推出來，不要在這裡抄一份
# ---------------------------------------------------------------------------
def _payload_keys() -> list[str]:
    """`save_macro` 裡 `payload = {...}` 那個字面值的鍵。

    抄一份清單在測試裡的話，之後加第六個欄位不會有任何東西變紅——而「多一個沒人
    驗的欄位」正是這支測試存在的原因。
    """
    source = pathlib.Path(gui.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, gui.__file__)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "save_macro"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and getattr(sub.targets[0], "id", "") == "payload"
                    and isinstance(sub.value, ast.Dict)):
                return [k.value for k in sub.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    raise AssertionError("在 save_macro 裡找不到 `payload = {...}` 字面值")


# 手改檔案寫得出來的東西。`Infinity` / `NaN` 是 `json.loads` 預設就收的擴充，而且
# `1e400` 連手改都不必——一個夠大的數字 parse 出來就是 `inf`。
_HOSTILE_JSON = [
    ("字串", '"abc"'),
    ("清單", "[1, 2]"),
    ("字典", '{"a": 1}'),
    ("布林", "true"),
    ("null", "null"),
    ("浮點", "3.7"),
    ("inf", "Infinity"),
    ("nan", "NaN"),
    ("超大數", "1e400"),
    ("負數", "-1"),
]

_HEALTHY_STEPS = ["wait 0", "wait 0", "wait 0"]


def _write(folder, *, field=None, raw="null"):
    """寫一份巨集檔；`field` 指定的那個欄位換成 `raw` 這段原始 JSON。"""
    payload = {
        "version": gui.MACRO_SCHEMA_VERSION,
        "name": "demo",
        "created": 0,
        "author_id": 0,
        "steps": list(_HEALTHY_STEPS),
    }
    text = json.dumps(payload, ensure_ascii=False)
    if field is not None:
        broken = dict(payload)
        broken[field] = "\x00PLACEHOLDER\x00"
        text = json.dumps(broken, ensure_ascii=False).replace(
            '"\\u0000PLACEHOLDER\\u0000"', raw)
    (folder / "demo.json").write_text(text, encoding="utf-8")


def _escaping_type(call):
    """回傳從 `call()` 逃出來的例外型別名；`GuiError` 與「沒有例外」都算合格。

    判斷的是**型別**而不是訊息：這條契約說的是「壞掉的檔案只能換來一句
    `GuiError`」，而 `GuiError` 的訊息本來就是這個專案自己寫的泛用句子。
    """
    try:
        call()
    except GuiError:
        return None
    # **不要寫成 `except BaseException`。** 這裡想抓的是「逃出去的例外型別」，而
    # 那些型別（`ValueError` / `TypeError` / `OverflowError`）全是 `Exception` 的
    # 子類別；把範圍放寬到 `BaseException` 只會多吃到兩種**不該被吃掉**的東西：
    # `asyncio.CancelledError`（吞掉取消訊號是本專案明文禁止的，
    # `test_exception_handlers.test_nothing_swallows_cancellation` 會紅），以及
    # pytest 自己的 `Skipped` / `Failed`——後者會被這支當成「逃出來的型別」記下來，
    # 於是一個跳過或失敗會偽裝成一筆合格的觀測值。
    except Exception as error:            # noqa: BLE001  這正是要抓的東西
        return type(error).__name__
    return None


# 巨集存取的入口。**每一個都要拿到一份新寫好的檔案**——`edit_macro` 成功時會把檔案
# 改掉，共用同一份輸入的話下一個入口看到的就不是我們安排的那個值了。
_ENTRY_POINTS = [
    ("load", lambda: gui.load_macro("demo")),
    ("edit", lambda: gui.edit_macro("demo", 1, "wait 1")),
    ("insert", lambda: gui.edit_macro("demo", 1, "wait 1", insert=True)),
    ("delete-line", lambda: gui.edit_macro("demo", 1, None)),
    ("list", lambda: gui.list_macros()),
]


@pytest.fixture()
def macro_dir(tmp_path, monkeypatch):
    """把巨集目錄指到 tmp。**不要讓任何一支測試碰到真的 `macros/`。**"""
    folder = tmp_path / "macros"
    folder.mkdir()
    monkeypatch.setattr(gui, "MACRO_DIR", folder)
    return folder


def test_the_field_list_really_comes_from_the_writer():
    """正面對照組：推導有推到東西，而且推到的是真的那幾個欄位。

    沒有這一格，`_payload_keys()` 回空清單時下面那支會「全部通過」——一個掃不到
    任何東西的迴圈跟一個乾淨的結果長得一模一樣。
    """
    keys = _payload_keys()
    assert len(keys) >= 5, keys
    assert {"steps", "author_id"} <= set(keys), keys


def test_the_escape_detector_can_actually_see_an_escape():
    """`_escaping_type` 自己的控制組：它要分得出三種結果。"""
    assert _escaping_type(lambda: None) is None
    assert _escaping_type(lambda: (_ for _ in ()).throw(GuiError("x"))) is None
    assert _escaping_type(lambda: int("abc")) == "ValueError"


@pytest.mark.parametrize("field", _payload_keys())
@pytest.mark.parametrize("label,raw", _HOSTILE_JSON, ids=[r[0] for r in _HOSTILE_JSON])
def test_every_field_the_store_writes_survives_being_hand_broken(
        macro_dir, field, label, raw):
    """`save_macro` 寫出去的欄位被改成任何 JSON 值，入口都只能丟 `GuiError`。

    ⚠️ 每個入口都重寫一次檔案：`edit_macro` 成功時會把檔案改掉，共用一份輸入的話
    後面的入口拿到的已經不是我們安排的值了（本 repo 為這個形狀吃過一次虧）。
    """
    for name, call in _ENTRY_POINTS:
        _write(macro_dir, field=field, raw=raw)
        escaped = _escaping_type(call)
        assert escaped is None, (
            f"`{field}` 被改成 {label}（{raw}）之後，`{name}` 丟出了 "
            f"{escaped}——手改壞的檔案只能換來一句 GuiError，"
            "而 /macro 那一側只 except GuiError")


def test_a_broken_author_id_does_not_become_a_real_user_id(macro_dir):
    """`author_id: true` 不得安靜地存成 1。

    `bool` 是 `int` 的子類別，所以 `int(True)` 是 1——一個真的存在的使用者 id。
    這是本專案同一個陷阱的第四個實例（前三個：兩個設定載入器、`_dorossi_new_session`
    的 `sTrue`）。壞掉的作者欄位應該退回 0（＝不知道是誰），不是退回某個人。
    """
    _write(macro_dir, field="author_id", raw="true")
    gui.edit_macro("demo", 1, "wait 1")
    saved = json.loads((macro_dir / "demo.json").read_text(encoding="utf-8"))
    assert saved["author_id"] == 0, (
        f"壞掉的 author_id 存回去變成 {saved['author_id']!r}")
    assert not isinstance(saved["author_id"], bool), "存回去的還是個 bool"


def test_a_healthy_macro_still_round_trips(macro_dir):
    """近似反例：把壞值擋掉不得變成「什麼都擋掉」。

    少了這一格，`load_macro` 改成無條件 `raise GuiError` 也是綠的，而那會讓整組
    /macro 指令全部失效——一個「安全方向」的缺陷同樣是缺陷。
    """
    _write(macro_dir)
    data = gui.load_macro("demo")
    assert data["steps"] == _HEALTHY_STEPS
    assert gui.edit_macro("demo", 1, "wait 1")[0] == "wait 1"
    assert [name for name, _steps, _mtime in gui.list_macros()] == ["demo"]


def test_a_real_author_id_is_kept(macro_dir):
    """另一個近似反例：正當的作者 id 不得被「正規化」掉。"""
    _write(macro_dir, field="author_id", raw="400000000000000001")
    gui.edit_macro("demo", 1, "wait 1")
    saved = json.loads((macro_dir / "demo.json").read_text(encoding="utf-8"))
    assert saved["author_id"] == 400000000000000001, saved["author_id"]


@pytest.mark.parametrize("label,raw", _HOSTILE_JSON, ids=[r[0] for r in _HOSTILE_JSON])
def test_load_macro_hands_back_a_normalised_author_id(macro_dir, label, raw):
    """讀取端自己就要正規化，不能只靠寫入端補救。

    ⚠️ 上面那幾支走的是 `edit_macro`（讀→改→寫），而 `save_macro` **也**會呼叫
    `_macro_author_id`——同一條路上有**兩道**正規化，留一道存回去的檔案就是對的。
    所以「只看存檔結果」的測試看不見讀取端被拿掉：實測把 `load_macro` 那一行改回
    `data.get("author_id")`，整組 62 支全綠（變異測試 2026-09-21 的 SURVIVED）。
    兩道互相遮蔽的正規化跟兩道互相遮蔽的守門是同一個形狀，破法也一樣——拿一個
    只碰得到其中一道的輸入，這裡就是**直接對 `load_macro` 的回傳值斷言**。

    讀取端那一道不是裝飾：`load_macro` 是公開函式，回的 dict 直接交給呼叫端，而
    下一個呼叫端不一定會再經過 `save_macro`（現在的 `/macro show`、`/macro run`
    都只讀 `["steps"]`，哪天多讀一個欄位不會有人想起這件事）。
    """
    _write(macro_dir, field="author_id", raw=raw)
    got = gui.load_macro("demo")["author_id"]
    assert isinstance(got, int) and not isinstance(got, bool), (
        f"`author_id` 是{label}（{raw}）時，`load_macro` 回的是 {got!r}——"
        "呼叫端拿到的就該是個普通的非負 int")
    assert got >= 0, f"`author_id` 是{label}（{raw}）時回了負數 {got!r}"


def test_a_negative_author_id_is_clamped_to_unknown(macro_dir):
    """負數不是任何人的 id——夾回 0（＝不知道是誰），不是原樣留著。

    `-1` 不會讓任何東西丟例外，所以上面那支「壞值只能換來 `GuiError`」的參數化
    測試對它是綠的。夾制那一行要自己有一支釘住，否則拿掉它沒有任何症狀
    （變異測試 2026-09-21 的第二個 SURVIVED）。
    """
    _write(macro_dir, field="author_id", raw="-1")
    assert gui.load_macro("demo")["author_id"] == 0
    gui.edit_macro("demo", 1, "wait 1")
    saved = json.loads((macro_dir / "demo.json").read_text(encoding="utf-8"))
    assert saved["author_id"] == 0, saved["author_id"]


# ---------------------------------------------------------------------------
# 宣告了上限，但拒絕那一邊從來沒被走過（分支覆蓋率 2026-09-21）
# ---------------------------------------------------------------------------
def test_load_macro_rejects_a_file_that_is_no_longer_a_macro(macro_dir):
    """`load_macro` 的四條退路：不存在／不是清單／空清單／步數超標／步不是字串。"""
    with pytest.raises(GuiError):
        gui.load_macro("demo")                      # 檔案根本不存在

    for raw in ('{"a": 1}', "[]", "0"):
        (macro_dir / "demo.json").write_text(
            json.dumps({"steps": json.loads(raw)}), encoding="utf-8")
        with pytest.raises(GuiError):
            gui.load_macro("demo")

    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["wait 0"] * (gui.MACRO_MAX_STEPS + 1)}),
        encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("demo")

    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["wait 0", 7]}), encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("demo")


def test_load_macro_revalidates_a_step_that_was_edited_by_hand(macro_dir):
    """docstring 承諾的那件事：磁碟上被改成不合法的一步，讀的時候就要被擋。

    重點在**讀的時候**。放過去的話，錯誤會等到重播到一半才出現——那時候鍵盤已經
    被打了一半。
    """
    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["wait 0", "click 不是座標 也不是座標"]}),
        encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("demo")

    # 區塊不平衡也一樣：單行都合法，但 `repeat` 少了 `end`。
    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["repeat 2", "wait 0"]}), encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("demo")


def test_edit_macro_refuses_a_line_outside_the_macro(macro_dir):
    """行號上下界，改與插各一組——兩條 `if` 的界線差一，所以要分開驗。"""
    _write(macro_dir)
    last = len(_HEALTHY_STEPS)
    for line in (0, last + 1):
        with pytest.raises(GuiError):
            gui.edit_macro("demo", line, "wait 1")
    for line in (0, last + 2):
        with pytest.raises(GuiError):
            gui.edit_macro("demo", line, "wait 1", insert=True)
    # 近似反例：邊界上的那兩個必須是合法的（插入可以插在最後一行的後面）。
    assert gui.edit_macro("demo", last, "wait 1")
    assert gui.edit_macro("demo", last + 1, "wait 1", insert=True)


def test_edit_macro_refuses_to_empty_a_macro(macro_dir):
    """刪到最後一步要擋下來，並且指向 `delete`——而不是留下一個空巨集檔。"""
    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["wait 0"]}), encoding="utf-8")
    with pytest.raises(GuiError):
        gui.edit_macro("demo", 1, None)
    assert gui.load_macro("demo")["steps"] == ["wait 0"], "擋下來了卻已經改掉檔案"


def test_edit_macro_refuses_to_grow_past_the_step_limit(macro_dir):
    """插入不得越過步數上限——上限是三份抄本，這是其中沒被跑過的那一份。"""
    (macro_dir / "demo.json").write_text(
        json.dumps({"steps": ["wait 0"] * gui.MACRO_MAX_STEPS}), encoding="utf-8")
    with pytest.raises(GuiError):
        gui.edit_macro("demo", 1, "wait 1", insert=True)


def test_the_repeat_count_has_to_be_inside_its_declared_range():
    """`repeat` 的次數上限是「一個打錯的數字能讓主機的鍵盤被打幾次」。"""
    gui.validate_macro_step("repeat 0")
    gui.validate_macro_step(f"repeat {gui.MACRO_MAX_REPEAT}")
    for bad in (-1, gui.MACRO_MAX_REPEAT + 1):
        with pytest.raises(GuiError):
            gui.validate_macro_step(f"repeat {bad}")


def test_write_host_file_refuses_more_bytes_than_it_declares(
        tmp_path, monkeypatch):
    """位元組上限，以及剛好在上限上的那一份要寫得進去。

    上限用 monkeypatch 縮小——真的做一份 64 MB 的 bytes 只是把同一條 `if` 跑得比較
    慢，而那個常數本來就是模組層讀的。
    """
    monkeypatch.setattr(gui, "PUT_MAX_BYTES", 8)
    target = tmp_path / "out.bin"
    with pytest.raises(GuiError):
        gui.write_host_file(str(target), b"x" * 9)
    assert not target.exists(), "被擋下來了卻已經寫出檔案"
    path, size = gui.write_host_file(str(target), b"x" * 8)
    assert size == 8 and path.name == "out.bin", (path, size)
    assert path.read_bytes() == b"x" * 8
