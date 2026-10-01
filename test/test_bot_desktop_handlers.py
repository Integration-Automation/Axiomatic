"""擁有者限定的桌面控制指令：`_gui_control` 前面那一層參數解析。

`/input click`、`/input mouse`、`/input type`、`/screen`、`/win`、`/proc launch`、
`/locate text|image|ui|gone|pixel`。擁有者閘在派發層（`_OWNER_ONLY_GROUPS`，由
`test_slash_gate` 守），處理函式本身不再判一次，所以這裡守的是另一半：**把使用者打的字
解析成哪一個桌面動作、帶哪些參數**，以及回覆不得帶出主機上的東西（視窗標題、UI 元素名稱
常含絕對路徑，`/proc launch` 的白名單項目可能是完整路徑）。

⚠️ 這台機器上隨時可能有正式批次或真桌面測試在跑，所以 **`_gui` 整個換成記錄器**
（`_RecordingGui`）：只有純解析函式（`parse_*`、`split_timeout`）與大寫的常數／例外類別
照用真的，其他每一個名字——包括這裡沒想到的——一律變成「記下參數、回設定值」的替身。
真模組的 `load_ac` 也換掉並在每支測試結束時確認沒被叫過，所以就算哪條路徑繞過了替身，
函式庫也載不起來。`/proc launch` 的 `subprocess` 與 `os.startfile` 同樣換掉；截圖與樣板
暫存檔寫在 `tmp_path`（`PROJECT_ROOT` 已導開）。
"""
from __future__ import annotations

import asyncio
import subprocess
import types
from pathlib import Path

import pytest

import _gui_control as real_gui
import discord_bot as b

_PURE = frozenset({"parse_xy", "parse_coord", "parse_size", "parse_button",
                   "parse_region", "parse_duration", "parse_color", "parse_tolerance",
                   "split_timeout"})


def _run(coro, timeout: float = 10.0):
    """跑一段 async 本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _where_called() -> str:
    """呼叫當下的執行緒上有沒有正在跑的事件迴圈：有＝`"loop"`，否則 `"thread"`。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "thread"
    return "loop"


class _RecordingGui:
    """`_gui` 的替身：純解析與大寫名字照用真的，其餘每一個名字都記錄、回設定值。

    `returns[name]` 可以是值或 callable（拿到同一組參數）；`raises[name]` 給一個例外就丟。
    記下呼叫當下在哪條執行緒，好斷言會卡住的桌面呼叫有沒有丟執行緒。"""

    def __init__(self, real) -> None:
        self._real = real
        self.calls: list = []
        self.returns: dict = {}
        self.raises: dict = {}

    def __getattr__(self, name: str):
        if name in _PURE or name[:1].isupper():
            return getattr(self._real, name)

        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs, _where_called()))
            if name in self.raises:
                raise self.raises[name]
            value = self.returns.get(name)
            return value(*args, **kwargs) if callable(value) else value

        return _call

    def names(self) -> list[str]:
        return [name for name, *_rest in self.calls]

    def only(self, name: str) -> tuple:
        """那個名字恰好被叫了一次：回 `(args, kwargs)`，並確認是在執行緒上叫的。"""
        hits = [(args, kwargs, where) for n, args, kwargs, where in self.calls if n == name]
        assert len(hits) == 1, (name, self.calls)
        args, kwargs, where = hits[0]
        assert where == "thread", f"`{name}` 在事件迴圈上被叫——會卡住整個 bot"
        return args, kwargs


@pytest.fixture
def gui(monkeypatch, tmp_path):
    fake = _RecordingGui(real_gui)
    monkeypatch.setattr(b, "_gui", fake)
    library_loads: list = []
    monkeypatch.setattr(real_gui, "load_ac", lambda *a, **k: library_loads.append(a))
    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(b, "SHOT_SETTLE_SEC", 0.0)
    yield fake
    assert library_loads == [], "真的桌面自動化函式庫被叫到了"


class _Replies(list):
    """`safe_reply` 的記錄器。附件當場讀出內容並關掉——真的送出也會關檔，留著不關的話
    處理函式在 `finally` 裡刪暫存檔會在 Windows 上失敗。"""

    def texts(self) -> list:
        return [content for content, _kw in self]


@pytest.fixture
def replies(monkeypatch):
    sent = _Replies()

    async def _reply(_message, content=None, **kwargs):
        upload = kwargs.get("file")
        if upload is not None:
            kwargs = dict(kwargs, file=(upload.filename, upload.fp.read()))
            upload.close()
        sent.append((content, kwargs))

    monkeypatch.setattr(b, "safe_reply", _reply)
    return sent


class _Attachment:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data


def _message(uid: int = 12345, attachments=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid),
                                 attachments=list(attachments or []))


def _writes_png(path, *args, **kwargs):
    """`capture` 的替身：真的寫一個檔，處理函式才看得到「有產出」。"""
    path.write_bytes(b"\x89PNG-fake")
    return path


# ---------------------------------------------------------------------------
# /input click
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,button,xy", [
    ("500 300", "mouse_left", (500, 300)),
    ("500,300 right", "mouse_right", (500, 300)),
    ("-120 -164 middle", "mouse_middle", (-120, -164)),
])
def test_click_sends_the_parsed_button_and_coordinates(gui, replies, payload, button, xy):
    """座標是整個虛擬桌面的座標，負的也合法（副螢幕在主螢幕左邊或上面）。"""
    _run(b.cmd_click(_message(), payload))
    args, _kw = gui.only("mouse_click")
    assert args == (button, *xy)
    assert replies.texts() == [f"🖱️ 已點選 ({xy[0]}, {xy[1]})"]


def test_click_relative_to_a_window_adds_its_origin(gui, replies):
    """`--win` 之後到句尾都是視窗片段（可含空白）；回覆不帶視窗標題。"""
    gui.returns["window_rect"] = (1, "D:\\secret\\proj - 記事本", (100, 200, 640, 480), 2)
    _run(b.cmd_click(_message(), "40 12 --win 未命名 - 記事本"))
    assert gui.only("window_rect")[0] == ("未命名 - 記事本",)
    assert gui.only("mouse_click")[0] == ("mouse_left", 140, 212)
    assert replies.texts() == ["🖱️ 已點選 (140, 212)（視窗相對座標）"]


def test_background_click_posts_to_the_window_and_says_it_may_be_ignored(gui, replies):
    _run(b.cmd_click(_message(), "40 12 right --bg --win 記事本"))
    assert gui.only("send_click_to_window")[0] == ("記事本", "mouse_right", 40, 12)
    assert "mouse_click" not in gui.names()
    assert replies.texts() == [
        "🖱️ 已把這一下送給那個視窗（背景，視窗相對座標 40, 12）。" + b.BACKGROUND_INPUT_CAVEAT]


@pytest.mark.parametrize("payload,reply_start", [
    ("500", "用法：`/input click <x> <y>"),
    ("1 2 3 4", "用法：`/input click <x> <y>"),
    ("40 12 --bg", "❌ `--bg` 要搭配 `--win <視窗標題片段>` 使用。"),
    ("70000 1", "❌ x must be between"),
    ("1 2 sideways", "❌ The mouse button must be"),
])
def test_click_refuses_bad_input_without_touching_the_desktop(gui, replies, payload,
                                                              reply_start):
    _run(b.cmd_click(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


def test_click_with_shot_attaches_a_capture_of_that_window(gui, replies, tmp_path):
    gui.returns["window_rect"] = (1, "t", (10, 20, 30, 40), 1)
    gui.returns["capture"] = _writes_png
    _run(b.cmd_click(_message(), "1 2 --shot --win 記事本"))
    args, kwargs = gui.only("capture")
    assert args[0].parent == tmp_path and kwargs == {"region": [10, 20, 40, 60]}
    assert replies[-1][1]["file"] == ("after.png", b"\x89PNG-fake")
    assert list(tmp_path.iterdir()) == [], "截圖暫存檔沒有刪掉"


# ---------------------------------------------------------------------------
# /input mouse
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,call,args,kwargs,reply", [
    ("move 5 6", "mouse_move", (5, 6), {}, "🖱️ 已移動到 (5, 6)"),
    ("click", "mouse_click", ("mouse_left", None, None), {}, "🖱️ 已在 目前位置 點 click 鍵"),
    ("right 7,8", "mouse_click", ("mouse_right", 7, 8), {}, "🖱️ 已在 (7, 8) 點 right 鍵"),
    ("dclick 1 2 middle", "mouse_click", ("mouse_middle", 1, 2), {"times": 2},
     "🖱️ 已在 (1, 2) 雙擊"),
    ("drag 1 2 3 4", "mouse_drag", (1, 2, 3, 4, "mouse_left"), {},
     "🖱️ 已從 (1, 2) 拖曳到 (3, 4)"),
    ("up right", "mouse_button_up", ("mouse_right", None, None), {}, "🖱️ 已放開滑鼠鍵"),
    ("scroll -3 10 20", "mouse_scroll", (-3, 10, 20), {}, "🖱️ 已捲動 -3 格"),
    ("scroll 2", "mouse_scroll", (2, None, None), {}, "🖱️ 已捲動 2 格"),
])
def test_mouse_sub_commands_map_to_the_right_call(gui, replies, payload, call, args, kwargs,
                                                  reply):
    _run(b.cmd_mouse(_message(), payload))
    assert gui.only(call) == (args, kwargs)
    assert replies.texts() == [reply]


def test_mouse_position_is_reported(gui, replies):
    gui.returns["mouse_position"] = (321, -45)
    _run(b.cmd_mouse(_message(), "pos"))
    gui.only("mouse_position")
    assert replies.texts() == ["🖱️ 游標在 (321, -45)"]


def test_mouse_down_schedules_the_automatic_release(gui, replies, monkeypatch):
    """按住不放會在主機上留下狀態，所以一定要排一個逾時自動放開；沒有登記到按下時刻
    （`input_pressed_at` 回 None）就不排。"""
    scheduled: list = []

    def _schedule(coro, *, label):
        scheduled.append(label)
        coro.close()

    monkeypatch.setattr(b, "_schedule_coro", _schedule)
    gui.returns["input_pressed_at"] = 123.0
    _run(b.cmd_mouse(_message(), "down 3 4"))
    assert gui.only("mouse_button_down")[0] == ("mouse_left", 3, 4)
    assert scheduled == ["auto-release-mouse"]
    assert "300 秒後自動放開" in replies.texts()[-1]
    gui.returns["input_pressed_at"] = None
    _run(b.cmd_mouse(_message(), "down"))
    assert scheduled == ["auto-release-mouse"]


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法："), ("wiggle", "用法："), ("scroll", "用法：`/input mouse scroll"),
    ("scroll up", "❌ 捲動格數必須是整數"), ("move 5", "❌ Two coordinate values are required"),
    ("dclick 1 2 nose", "❌ The mouse button must be"),
])
def test_mouse_refuses_bad_input_without_touching_the_desktop(gui, replies, payload,
                                                              reply_start):
    _run(b.cmd_mouse(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


# ---------------------------------------------------------------------------
# /input type
# ---------------------------------------------------------------------------
def test_type_sends_the_text_verbatim(gui, replies):
    """原樣送出，連前後空白都不動——使用者要打的可能就是那個空白。"""
    text = "  hello, " + "w" * 40 + "  "
    _run(b.cmd_type(_message(), text))
    assert gui.only("type_text")[0] == (text,)
    assert replies.texts() == [f"⌨️ 已輸入 {len(text)} 字（前 30 字：`{text[:30]}`）"]


def test_type_without_text_or_with_a_locked_desktop_explains(gui, replies):
    _run(b.cmd_type(_message(), ""))
    gui.raises["type_text"] = real_gui.GuiError("The computer is currently locked.")
    _run(b.cmd_type(_message(), "x"))
    assert replies.texts() == ["用法：`/input type <text>` — 例如 `/input type hello world`",
                               "❌ The computer is currently locked."]


# ---------------------------------------------------------------------------
# /screen
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,kwargs", [
    ("", {"region": None, "all_screens": False}),
    ("all", {"region": None, "all_screens": True}),
    ("10 20 30 40", {"region": [10, 20, 40, 60], "all_screens": False}),
])
def test_screen_uploads_the_capture_and_deletes_the_file(gui, replies, tmp_path, payload,
                                                         kwargs):
    gui.returns["capture"] = _writes_png
    _run(b.cmd_screen(_message(), payload))
    args, got = gui.only("capture")
    assert args[0].parent == tmp_path and args[0].suffix == ".png"
    assert got == kwargs
    assert replies == [(None, {"file": ("screen.png", b"\x89PNG-fake")})]
    assert list(tmp_path.iterdir()) == []


def test_screen_of_a_window_captures_its_rectangle(gui, replies):
    gui.returns["window_rect"] = (9, "C:\\Users\\me\\x.py", (100, 50, 300, 200), 1)
    gui.returns["capture"] = _writes_png
    _run(b.cmd_screen(_message(), "win 記事 本"))
    assert gui.only("window_rect")[0] == ("記事 本",)
    assert gui.only("capture")[1]["region"] == [100, 50, 400, 250]


@pytest.mark.parametrize("payload,reply_start", [
    ("win", "用法：`/screen window <視窗標題片段>`"),
    ("10 20", "❌ A region needs four values"),
    ("10 20 0 5", "❌ width must be greater than 0."),
])
def test_screen_refuses_a_bad_target_without_capturing(gui, replies, payload, reply_start):
    _run(b.cmd_screen(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


def test_screen_says_so_when_the_capture_produced_nothing(gui, replies):
    _run(b.cmd_screen(_message(), ""))
    assert replies.texts() == ["❌ 截圖沒有產出檔案"]
    gui.raises["capture"] = real_gui.GuiError("Screenshot failed.")
    _run(b.cmd_screen(_message(), ""))
    assert replies.texts()[-1] == "❌ Screenshot failed."


def test_screen_info_reports_the_layout_and_input_state(gui, replies):
    gui.returns.update(screen_info={"primary": (1920, 1080), "virtual": (0, -164, 3456, 1244),
                                    "monitors": 2},
                       input_desktop_available=True, input_reaches_system=True)
    _run(b.cmd_screen(_message(), "info"))
    assert replies.texts() == [
        "🖥️ 主螢幕：1920 × 1080\n整體桌面：3456 × 1244，左上角在 (0, -164)／共 2 個螢幕\n"
        "座標可以是負的——`/input click` / `/input mouse` 吃的就是這組座標。"]
    assert "capture" not in gui.names()


def test_screen_info_warns_about_a_locked_desktop_before_probing_input(gui, replies):
    """鎖定時不必再送那一下探測輸入（`elif`）——送了也進不去。"""
    gui.returns.update(screen_info={"primary": (800, 600)}, input_desktop_available=False)
    _run(b.cmd_screen(_message(), "info"))
    assert replies.texts() == ["🖥️ 主螢幕：800 × 600\n⚠️ 電腦目前是鎖定狀態，送不進滑鼠鍵盤操作。"]
    assert "input_reaches_system" not in gui.names()
    gui.returns.update(input_desktop_available=True, input_reaches_system=False)
    _run(b.cmd_screen(_message(), "info"))
    assert "**沒有進到系統**" in replies.texts()[-1]


def test_screen_gif_records_with_the_parsed_options(gui, replies, tmp_path):
    def _gif(path, **kwargs):
        path.write_bytes(b"GIF89a")
        return 8

    gui.returns["capture_gif"] = _gif
    _run(b.cmd_screen(_message(), "gif 4 2 --in 0 0 10 10"))
    args, kwargs = gui.only("capture_gif")
    assert args[0].parent == tmp_path and args[0].suffix == ".gif"
    assert kwargs == {"seconds": 4.0, "fps": 2.0, "region": [0, 0, 10, 10]}
    assert replies == [("🎞️ 8 影格 / 4 秒", {"file": ("screen.gif", b"GIF89a")})]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload,first_line", [
    ("gif 4 fast", "❌ 參數不合法"),
    ("gif 99", f"❌ The maximum is {real_gui.GIF_MAX_SECONDS:g} seconds."),
])
def test_screen_gif_rejects_bad_options_without_the_raw_error(gui, replies, payload,
                                                             first_line):
    """`float()` 丟的原始英文訊息不得送出（它是不受控的內部字串），只回泛用句加用法。"""
    _run(b.cmd_screen(_message(), payload))
    first, _, usage = replies.texts()[-1].partition("\n")
    assert first == first_line and usage.startswith("用法：`/screen gif")
    assert "could not convert" not in replies.texts()[-1]
    assert gui.calls == []


# ---------------------------------------------------------------------------
# /win focus、/win list、/win …
# ---------------------------------------------------------------------------
def test_focus_lowercases_the_needle_and_counts_other_matches(gui, replies):
    gui.returns["window_focus"] = (1, "D:\\secret\\title", 3)
    _run(b.cmd_focus(_message(), "  NotePad "))
    assert gui.only("window_focus")[0] == ("notepad",)
    assert replies.texts() == ["🪟 已把符合 `notepad` 的視窗拉到前景（另外 2 個視窗也命中）"]


def test_focus_without_a_needle_or_a_match_explains(gui, replies):
    _run(b.cmd_focus(_message(), "  "))
    gui.raises["window_focus"] = real_gui.GuiError("No matching window was found.")
    _run(b.cmd_focus(_message(), "x"))
    assert replies.texts()[0].startswith("用法：`/win focus <substring>`")
    assert replies.texts()[1] == "❌ No matching window was found."


def test_window_list_filters_and_scrubs_host_paths_from_titles(gui, replies):
    """標題列幾乎一定帶絕對路徑（編輯器、檔案總管），送出前要刷掉。"""
    gui.returns["list_windows"] = [
        (1, "C:\\Users\\me\\proj\\main.py - Editor"), (2, "Calculator"),
        (3, "notes - EDITOR " + "x" * 100)]
    _run(b.cmd_windows(_message(), "editor"))
    reply = replies.texts()[-1]
    lines = reply.split("\n")
    assert lines[0] == "🪟 可見視窗 2 個"
    assert "C:\\Users" not in reply and "main.py" not in reply and "[path]" in lines[1]
    assert lines[2].startswith("2. notes - EDITOR") and len(lines[2]) == len("2. ") + 70
    assert "Calculator" not in reply


def test_window_list_caps_at_thirty_and_says_when_nothing_matches(gui, replies):
    gui.returns["list_windows"] = [(i, f"w{i}") for i in range(35)]
    _run(b.cmd_windows(_message(), ""))
    assert replies.texts()[0].startswith("🪟 可見視窗 35 個（顯示前 30 個）\n1. w0\n")
    assert "\n30. w29" in replies.texts()[0] and "w30" not in replies.texts()[0]
    _run(b.cmd_windows(_message(), "nothing-like-this"))
    assert replies.texts()[-1] == "沒有符合的可見視窗"


@pytest.mark.parametrize("payload,call,args,reply", [
    ("snap LEFT 記事 本", "snap_window", ("記事 本", "LEFT"), "🪟 已靠到 `left`"),
    ("grid a | b c | ", "grid_windows", (["a", "b c"],), "🪟 已把 4 個視窗排成方格"),
    ("move 0 0 1280 720 note pad", "window_move", ("note pad", 0, 0, 1280, 720),
     "🪟 已搬到 (0, 0)、大小 1280 × 720（命中 4 個）"),
    ("move -5 7 notepad", "window_move", ("notepad", -5, 7, None, None),
     "🪟 已搬到 (-5, 7)（命中 4 個）"),
    ("close 記事本", "window_close", ("記事本",), "🪟 已送出關閉（命中 4 個，處理第一個）"),
    ("focus 記事本", "window_focus", ("記事本",), "🪟 已拉到前景（命中 4 個）"),
    ("min 記事本", "window_show", ("記事本", "min"), "🪟 已 `min`（命中 4 個）"),
    ("wait 10 記事 本", "wait_window", ("記事 本", 10.0), "🪟 視窗已出現"),
    ("wait 記事本", "wait_window", ("記事本", 30.0), "🪟 視窗已出現"),
])
def test_window_actions_map_to_the_right_call_and_never_echo_titles(gui, replies, payload,
                                                                    call, args, reply):
    secret = "C:\\Users\\me\\private.txt - 記事本"
    gui.returns.update(snap_window=(1, secret), grid_windows=4,
                       window_move=(1, secret, 4), window_close=(1, secret, 4),
                       window_focus=(1, secret, 4), window_show=(1, secret, 4))
    _run(b.cmd_win(_message(), payload))
    assert gui.only(call)[0] == args
    assert replies.texts() == [reply]


def test_window_position_gives_coordinates_and_a_capture_hint(gui, replies):
    gui.returns["window_rect"] = (1, "C:\\x\\secret.py", (10, 20, 300, 200), 2)
    _run(b.cmd_win(_message(), "pos 記事本"))
    reply = replies.texts()[-1]
    assert reply == ("🪟 位置 (10, 20)　大小 300 × 200　中心 (160, 120)（命中 2 個）\n"
                     "`/screen main 10 20 300 200` 可以只截這個視窗。")


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法：`/win state"), ("close", "用法：`/win state"),
    ("teleport x", "用法：`/win state"), ("snap left", "用法：`/win snap"),
    ("grid onlyone", "用法：`/win grid"), ("move 1 2 3 x", "用法：`/win move"),
    ("move 1 2", "用法：`/win move"), ("wait nan 記事本", "❌ Seconds must be a finite number."),
])
def test_window_refuses_bad_input_without_touching_the_desktop(gui, replies, payload,
                                                               reply_start):
    _run(b.cmd_win(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


def test_window_layout_save_restore_remove_and_list(gui, replies):
    gui.returns.update(save_window_layout=5, restore_window_layout=3,
                       list_window_layouts=[("desk", 5, 0.0)])
    for payload in ("layout save desk", "layout restore desk", "layout rm desk",
                    "layout list"):
        _run(b.cmd_win(_message(), payload))
    assert gui.only("save_window_layout")[0] == ("desk",)
    assert gui.only("restore_window_layout")[0] == ("desk",)
    assert gui.only("delete_window_layout")[0] == ("desk",)
    texts = replies.texts()
    assert texts[:3] == ["🗂️ 已存版面 `desk`（5 個視窗）", "🗂️ 版面 `desk`：已擺回 3 個視窗",
                         "🗑️ 已刪除版面 `desk`"]
    assert texts[3].startswith("🗂️ 視窗版面 1 個：\n• `desk`（5 個視窗，")


def test_window_layout_with_nothing_saved_or_no_name(gui, replies):
    gui.returns["list_window_layouts"] = []
    _run(b.cmd_win(_message(), "layout list"))
    _run(b.cmd_win(_message(), "layout save"))
    assert replies.texts()[0] == "目前沒有存過視窗版面"
    assert replies.texts()[1].startswith("用法：`/win layout save <名稱>`")
    assert "save_window_layout" not in gui.names()


# ---------------------------------------------------------------------------
# /proc launch
# ---------------------------------------------------------------------------
@pytest.fixture
def launcher(monkeypatch):
    state = types.SimpleNamespace(popen=[], startfile=[], popen_raises=None)

    class _Proc:
        pid = 4242

    def _popen(target, **kwargs):
        state.popen.append((target, kwargs))
        if state.popen_raises is not None:
            raise state.popen_raises
        return _Proc()

    monkeypatch.setattr(b, "subprocess", types.SimpleNamespace(
        Popen=_popen, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr(b.os, "startfile", lambda target: state.startfile.append(target))
    monkeypatch.setattr(b, "GUI_LAUNCH_WHITELIST", ["C:/Windows/notepad.exe", "calc.exe"])
    monkeypatch.setattr(b, "GUI_LAUNCH_ALIASES", {"Steam Game": "steam://rungameid/1",
                                                  "editor": "D:/Tools/edit.exe"})
    return state


def test_launch_starts_a_whitelisted_path_by_its_basename(launcher, replies):
    _run(b.cmd_launch(_message(), "Notepad"))
    (target, kwargs), = launcher.popen
    assert target == "C:/Windows/notepad.exe"
    assert kwargs == {"shell": False, "stdin": subprocess.DEVNULL,
                      "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    assert replies.texts() == ["🚀 已啟動 `Notepad`"]


def test_launch_passes_a_bare_name_as_an_argument_list(launcher, replies):
    _run(b.cmd_launch(_message(), "calc"))
    assert launcher.popen[0][0] == ["calc.exe"]


def test_launch_hands_a_uri_alias_to_the_os_and_replies_with_the_name_typed(launcher,
                                                                           replies):
    """回覆用使用者打的名字，不是解析後的目標——目標可能是主機路徑。"""
    _run(b.cmd_launch(_message(), "steam game"))
    _run(b.cmd_launch(_message(), "editor"))
    assert launcher.startfile == ["steam://rungameid/1"]
    assert launcher.popen[0][0] == "D:/Tools/edit.exe"
    assert replies.texts() == ["🚀 開啟 `steam game`", "🚀 已啟動 `editor`"]


def test_launch_of_an_unknown_name_lists_basenames_never_paths(launcher, replies,
                                                              monkeypatch):
    monkeypatch.setattr(b, "GUI_LAUNCH_WHITELIST",
                        [f"C:/Apps/tool{i}.exe" for i in range(10)])
    _run(b.cmd_launch(_message(), "rm -rf"))
    reply = replies.texts()[-1]
    assert reply.startswith("❌ `rm -rf` 不在 launch_whitelist / launch_aliases。允許名稱："
                            "`Steam Game`, `editor`, `tool0.exe`")
    assert "C:/Apps" not in reply and reply.endswith(" 等 12 筆")
    assert launcher.popen == [] and launcher.startfile == []


def test_launch_is_disabled_with_empty_lists(launcher, replies, monkeypatch):
    monkeypatch.setattr(b, "GUI_LAUNCH_WHITELIST", [])
    monkeypatch.setattr(b, "GUI_LAUNCH_ALIASES", {})
    _run(b.cmd_launch(_message(), "calc"))
    assert replies.texts()[-1].startswith("❌ `/proc launch` 未啟用")
    assert launcher.popen == []


def test_launch_failure_names_only_the_error_type(launcher, replies, capsys):
    launcher.popen_raises = FileNotFoundError(2, "missing", "C:/Windows/notepad.exe")
    _run(b.cmd_launch(_message(), "notepad"))
    assert replies.texts() == ["❌ 啟動 `notepad` 失敗：`FileNotFoundError`"]
    assert "C:/Windows/notepad.exe" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# /screen pixel
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", ["100 200", "100,200"])
def test_pixel_reports_rgb_and_hex(gui, replies, payload):
    gui.returns["pixel_color"] = (255, 0, 16)
    _run(b.cmd_pixel(_message(), payload))
    assert gui.only("pixel_color")[0] == (100, 200)
    assert replies.texts() == ["🎨 (100, 200) = RGB(255, 0, 16) / `#FF0010`"]


@pytest.mark.parametrize("payload,reply_start", [
    ("100", "用法：`/screen pixel"), ("a b", "❌ x must be an integer.")])
def test_pixel_refuses_bad_coordinates(gui, replies, payload, reply_start):
    _run(b.cmd_pixel(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start) and gui.calls == []


# ---------------------------------------------------------------------------
# /locate text find|click|wait
# ---------------------------------------------------------------------------
def test_find_text_passes_region_and_language_and_scrubs_the_hits(gui, replies):
    hits = [{"text": "C:\\Users\\me\\secret.txt", "x": 5, "y": 6, "confidence": 91.4}]
    hits += [{"text": f"確定{i}", "x": i, "y": i, "confidence": 50} for i in range(11)]
    gui.returns["find_text"] = hits
    _run(b.cmd_find_text(_message(), "--in 0 0 100 50 --lang jpn 確定"))
    args, kwargs = gui.only("find_text")
    assert args == ("確定",) and kwargs == {"lang": "jpn", "region": [0, 0, 100, 50]}
    reply = replies.texts()[-1]
    assert reply.startswith("🔍 找到 12 處：\n• `[path]` → (5, 6)　信心 91%")
    assert "secret" not in reply and reply.endswith("…另有 2 處")


@pytest.mark.parametrize("count,tail", [(10, None), (11, "…另有 1 處")])
def test_find_text_lists_ten_hits_and_counts_the_rest(gui, replies, count, tail):
    gui.returns["find_text"] = [{"text": f"t{i}", "x": i, "y": i, "confidence": 80}
                                for i in range(count)]
    _run(b.cmd_find_text(_message(), "t"))
    lines = replies.texts()[-1].split("\n")
    assert sum(line.startswith("• ") for line in lines) == 10
    if tail is None:
        assert lines[-1].startswith("• "), lines
    else:
        assert lines[-1] == tail, lines


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法：`/locate text find"), ("--in 0 0 100", "❌ `--in` 需要 4 個值。"),
    ("--in 0 0 0 5 確定", "❌ width must be greater than 0.")])
def test_find_text_refuses_bad_input(gui, replies, payload, reply_start):
    _run(b.cmd_find_text(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start) and gui.calls == []


def test_find_text_with_no_match_says_so(gui, replies):
    gui.returns["find_text"] = []
    _run(b.cmd_find_text(_message(), "確定"))
    assert replies.texts() == ["🔍 畫面上找不到這段文字"]


def test_click_text_clicks_and_can_attach_a_shot(gui, replies):
    gui.returns.update(click_text=(30, 40), capture=_writes_png)
    _run(b.cmd_click_text(_message(), "--shot --lang chi_tra 存檔"))
    args, kwargs = gui.only("click_text")
    assert args == ("存檔",) and kwargs == {"lang": "chi_tra", "region": None}
    assert replies.texts()[0] == "🖱️ 已點選畫面上的文字 → (30, 40)"
    assert replies[1][1]["file"][0] == "after.png"


def test_click_text_without_a_target_shows_usage(gui, replies):
    _run(b.cmd_click_text(_message(), "--shot"))
    assert replies.texts()[-1].startswith("用法：`/locate text click") and gui.calls == []


@pytest.mark.parametrize("payload,args", [
    ("10 完成 了", ("完成 了", 10.0)), ("完成", ("完成", 30.0))])
def test_wait_text_parses_the_leading_timeout(gui, replies, payload, args):
    gui.returns["wait_text"] = (7, 8)
    _run(b.cmd_wait_text(_message(), payload))
    got, kwargs = gui.only("wait_text")
    assert got == args and kwargs == {"lang": None, "region": None}
    assert replies.texts() == ["✅ 文字已出現 → (7, 8)"]


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法：`/locate text wait [秒] [--in"), ("999 完成", "❌ The maximum is 120 seconds."),
    ("nan 完成", "❌ Seconds must be a finite number.")])
def test_wait_text_refuses_bad_input(gui, replies, payload, reply_start):
    _run(b.cmd_wait_text(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start) and gui.calls == []


# ---------------------------------------------------------------------------
# /locate image click|find|wait 與 /locate gone image（附件當樣板）
# ---------------------------------------------------------------------------
class _TemplateSpy:
    """樣板圖的替身動作：記下收到的路徑與當下的檔案內容。"""

    def __init__(self, result) -> None:
        self.seen: list = []
        self.result = result

    def __call__(self, path, *args, **kwargs):
        self.seen.append((Path(path), Path(path).read_bytes(), args, kwargs))
        return self.result


@pytest.mark.parametrize("handler,call,payload,result,args,kwargs,reply", [
    ("cmd_click_image", "click_image", "0.7", (11, 22), (), {"threshold": 0.7},
     "🖱️ 已點選圖片中心 → (11, 22)"),
    ("cmd_click_image", "click_image", "", (11, 22), (), {"threshold": 0.9},
     "🖱️ 已點選圖片中心 → (11, 22)"),
    ("cmd_wait_image", "wait_image", "45 0.8", (1, 2), (45.0,), {"threshold": 0.8},
     "✅ 圖片已出現 → (1, 2)"),
    ("cmd_wait_image", "wait_image", "0.8", (1, 2), (30.0,), {"threshold": 0.8},
     "✅ 圖片已出現 → (1, 2)"),
    ("cmd_wait_image", "wait_image", "45", (1, 2), (45.0,), {"threshold": 0.9},
     "✅ 圖片已出現 → (1, 2)"),
    ("cmd_wait_gone", "wait_image_gone", "image 12 0.6", None, (12.0,),
     {"threshold": 0.6}, "✅ 那張圖已經從畫面上消失"),
    # 只給一個數字：跟 `/locate image wait` 同一種讀法——0.8 是門檻、秒數照預設；
    # 45 是秒數、門檻照預設。舊的讀法把 0.8 當成等 0.8 秒。
    ("cmd_wait_gone", "wait_image_gone", "image 0.8", None, (30.0,),
     {"threshold": 0.8}, "✅ 那張圖已經從畫面上消失"),
    ("cmd_wait_gone", "wait_image_gone", "image 45", None, (45.0,),
     {"threshold": 0.9}, "✅ 那張圖已經從畫面上消失"),
])
def test_image_locators_use_the_attachment_as_the_template(gui, replies, tmp_path, handler,
                                                          call, payload, result, args,
                                                          kwargs, reply):
    """樣板一律從附件拿（不接受主機路徑），存成暫存檔交給定位，用完一定刪掉。"""
    spy = _TemplateSpy(result)
    gui.returns[call] = spy
    _run(getattr(b, handler)(_message(attachments=[_Attachment(b"tpl-bytes")]), payload))
    (path, data, got_args, got_kwargs), = spy.seen
    assert path.parent == tmp_path and data == b"tpl-bytes"
    assert (got_args, got_kwargs) == (args, kwargs)
    gui.only(call)
    assert replies.texts() == [reply]
    assert list(tmp_path.iterdir()) == []


def test_find_image_lists_hits_without_clicking(gui, replies):
    gui.returns["locate_image_all"] = _TemplateSpy([(i, i * 2, 0.95) for i in range(12)])
    _run(b.cmd_find_image(_message(attachments=[_Attachment(b"t")]), "0.5"))
    assert gui.names() == ["locate_image_all"]
    reply = replies.texts()[-1]
    assert reply.startswith("🔍 找到 12 處：\n• (0, 0)　相似度 0.95")
    assert reply.endswith("…另有 2 處")
    gui.returns["locate_image_all"] = _TemplateSpy([])
    _run(b.cmd_find_image(_message(attachments=[_Attachment(b"t")]), ""))
    assert replies.texts()[-1].startswith("🔍 畫面上找不到這張圖")


@pytest.mark.parametrize("handler,payload,reply_start", [
    ("cmd_click_image", "2", "❌ 門檻必須介於 0.1 到 1.0。"),
    ("cmd_find_image", "abc", "❌ 門檻必須是 0.1 到 1.0 之間的數字。"),
    ("cmd_wait_image", "soon", "❌ 秒數必須是數字。"),
    ("cmd_wait_image", "700 0.9", "❌ The maximum is 600 seconds."),
    ("cmd_wait_gone", "image soon", "❌ 秒數必須是數字。"),
    ("cmd_wait_gone", "image 700 0.9", "❌ The maximum is 600 seconds."),
    ("cmd_wait_gone", "image 0.05", "❌ 門檻必須介於 0.1 到 1.0。"),
])
def test_image_locators_reject_bad_options_and_still_clean_up(gui, replies, tmp_path,
                                                              handler, payload, reply_start):
    _run(getattr(b, handler)(_message(attachments=[_Attachment(b"t")]), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == [] and list(tmp_path.iterdir()) == []


# 兩個指令的參數讀法必須逐格一樣：各自有測試、各自是綠的，不等於兩邊說的是同一件事。
_IMAGE_WAIT_CORPUS = ["", "0.8", "45", "45 0.8", "1", "1 0.5", "1.5", "600", "0.1"]


@pytest.mark.parametrize("payload", _IMAGE_WAIT_CORPUS)
def test_image_wait_and_image_gone_read_the_arguments_identically(gui, replies, payload):
    seen = {}
    for handler, call, text in (("cmd_wait_image", "wait_image", payload),
                                ("cmd_wait_gone", "wait_image_gone", f"image {payload}")):
        spy = _TemplateSpy((1, 2))
        gui.returns[call] = spy
        _run(getattr(b, handler)(_message(attachments=[_Attachment(b"t")]), text))
        (_path, _data, args, kwargs), = spy.seen
        seen[handler] = (args, kwargs)
    assert seen["cmd_wait_image"] == seen["cmd_wait_gone"], seen


def test_the_shared_image_wait_parser_reads_one_number_by_its_size():
    """單獨一個數字：大於 1 是秒數、其餘是門檻；`1` 是兩者唯一重疊的值，讀成門檻。"""
    parse = b._parse_image_wait_args
    assert parse("") == (30.0, b.LOCATE_THRESHOLD_DEFAULT)
    assert parse("0.8") == (30.0, 0.8)
    assert parse("1") == (30.0, 1.0)
    assert parse("1.5") == (1.5, b.LOCATE_THRESHOLD_DEFAULT)
    assert parse("1 0.5") == (1.0, 0.5)
    assert parse("12", default_timeout=7.0) == (12.0, b.LOCATE_THRESHOLD_DEFAULT)
    assert parse("0.5", default_timeout=7.0) == (7.0, 0.5)


def test_the_slash_image_wait_sends_both_numbers_so_one_second_is_not_a_threshold(
        monkeypatch):
    """斜線指令的 `seconds:1` 沒帶門檻時，以前只送一個 `1`，被讀成門檻 1.0、等 30 秒。"""
    sent: list = []

    async def _record(_interaction, _attachment, handler, payload, **_kwargs):
        sent.append((handler, payload))

    monkeypatch.setattr(b, "_slash_run_with_file", _record)
    _run(b.slash_locate_image_wait.callback(object(), object(), 1))
    _run(b.slash_locate_image_wait.callback(object(), object(), 1, 0.5))
    assert [handler for handler, _payload in sent] == [b.cmd_wait_image] * 2
    assert [b._parse_image_wait_args(payload) for _handler, payload in sent] == [
        (1.0, b.LOCATE_THRESHOLD_DEFAULT), (1.0, 0.5)]


@pytest.mark.parametrize("handler", ["cmd_click_image", "cmd_find_image", "cmd_wait_image"])
def test_image_locators_without_an_attachment_show_usage(gui, replies, tmp_path, handler):
    _run(getattr(b, handler)(_message(), ""))
    assert replies.texts()[-1].startswith("用法：上傳一張") and gui.calls == []
    assert list(tmp_path.iterdir()) == []


def test_an_unexpected_locator_failure_is_generic_for_strangers(gui, replies, capsys):
    gui.raises["click_image"] = RuntimeError("cv2 failed at D:/secret/x.png")
    _run(b.cmd_click_image(_message(attachments=[_Attachment(b"t")]), ""))
    _run(b.cmd_click_image(_message(b.OWNER_USER_ID, [_Attachment(b"t")]), ""))
    assert replies.texts() == ["❌ 圖片定位失敗",
                               "RuntimeError: cv2 failed at D:/secret/x.png"]
    assert "image locate unexpected" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# /locate ui
# ---------------------------------------------------------------------------
def test_ui_find_parses_type_and_window_and_scrubs_names(gui, replies):
    gui.returns["ui_find"] = [
        {"type": "button", "name": "C:\\Users\\me\\a.txt", "x": 1, "y": 2, "width": 30,
         "height": 10, "enabled": False},
        {"type": "button", "name": "OK", "x": 3, "y": 4, "width": 5, "height": 6,
         "enabled": True}]
    _run(b.cmd_ui(_message(), "find 存 檔 --type button --win 未命名 - 記事本"))
    args, kwargs = gui.only("ui_find")
    assert args == ("存 檔",) and kwargs == {"control_type": "button",
                                             "window": "未命名 - 記事本"}
    assert replies.texts() == ["🧩 找到 2 個：\n"
                               "• [button] `[path]` @ (1, 2)　30×10（停用中）\n"
                               "• [button] `OK` @ (3, 4)　5×6"]


def test_ui_find_without_a_window_explains_the_narrow_scan(gui, replies):
    gui.returns["ui_find"] = []
    _run(b.cmd_ui(_message(), "find 存檔"))
    assert gui.only("ui_find")[1] == {"control_type": "", "window": ""}
    assert replies.texts() == ["🧩 找不到這個名稱的 UI 元素" + b._ui_scope_hint("")]
    _run(b.cmd_ui(_message(), "find 存檔 --win x"))
    assert replies.texts()[-1] == "🧩 找不到這個名稱的 UI 元素"


def test_ui_read_reports_each_kind_of_value_and_hides_passwords(gui, replies):
    gui.returns["ui_value"] = [
        {"type": "edit", "name": "pw", "x": 1, "y": 1, "password": True},
        {"type": "edit", "name": "path", "x": 2, "y": 2, "value": "D:\\secret\\x",
         "read_only": True},
        {"type": "checkbox", "name": "c", "x": 3, "y": 3, "toggle": "mixed"},
        {"type": "listitem", "name": "l", "x": 4, "y": 4, "selected": False},
        {"type": "slider", "name": "s", "x": 5, "y": 5, "number": 42.0},
        {"type": "pane", "name": "p", "x": 6, "y": 6}]
    _run(b.cmd_ui(_message(), "read 欄位"))
    lines = replies.texts()[-1].split("\n")
    assert lines[0] == "🧩 讀到 6 個："
    assert lines[1].endswith("密碼欄位（不回傳內容）")
    assert lines[2].endswith("值 `[path]`（唯讀）") and "secret" not in lines[2]
    assert lines[3].endswith("部分勾選") and lines[4].endswith("未選取")
    assert lines[5].endswith("數值 42") and lines[6].endswith("這個元素沒有可讀的值")


def test_ui_tree_click_wait_and_gone(gui, replies, tmp_path):
    gui.returns.update(
        ui_tree=[{"type": "button", "name": "C:\\x\\y.txt", "x": 1, "y": 2}],
        ui_click=(9, 8), ui_wait={"x": 7, "y": 6}, capture=_writes_png,
        window_rect=(1, "C:\\x\\title", (0, 0, 10, 10), 1))
    _run(b.cmd_ui(_message(), "tree 記事本"))
    _run(b.cmd_ui(_message(), "click 確定 --shot --win 記事本"))
    _run(b.cmd_ui(_message(), "wait 15 確定"))
    _run(b.cmd_ui(_message(), "gone 確定"))
    assert gui.only("ui_tree")[0] == ("記事本",)
    assert gui.only("ui_click") == (("確定",), {"control_type": "", "window": "記事本"})
    waits = [(args, kwargs) for n, args, kwargs, _w in gui.calls if n == "ui_wait"]
    assert waits == [(("確定", 15.0), {"control_type": "", "window": "", "gone": False}),
                     (("確定", 30.0), {"control_type": "", "window": "", "gone": True})]
    texts = replies.texts()
    assert texts[0] == "🧩 UI 元素 1 個：\n• [button] `[path]` @ (1, 2)"
    assert texts[1] == "🖱️ 已點選 UI 元素 → (9, 8)" and replies[2][1]["file"][0] == "after.png"
    assert texts[3:] == ["✅ UI 元素已出現 → (7, 6)", "✅ 那個 UI 元素已經消失"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法：\n• `/locate ui find"), ("poke x", "用法：\n• `/locate ui find"),
    ("find", "用法：`/locate ui find <名稱>`"), ("read", "用法：`/locate ui read <名稱>`"),
    ("click --shot", "用法：`/locate ui click"), ("wait", "用法：`/locate ui wait [秒] <名稱>`"),
    ("find x --type", "❌ `--type` 需要 1 個值。"),
])
def test_ui_refuses_bad_input_without_touching_the_desktop(gui, replies, payload,
                                                           reply_start):
    _run(b.cmd_ui(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


def test_ui_tree_of_an_unsupported_program_points_elsewhere(gui, replies):
    gui.returns["ui_tree"] = []
    _run(b.cmd_ui(_message(), "tree"))
    assert replies.texts()[-1].startswith("沒有讀到任何 UI 元素。")


# ---------------------------------------------------------------------------
# /locate gone text|window
# ---------------------------------------------------------------------------
def test_wait_gone_text_and_window(gui, replies):
    _run(b.cmd_wait_gone(_message(), "text 20 --in 0 0 50 50 --lang eng 處理 中"))
    _run(b.cmd_wait_gone(_message(), "window 安裝 程式"))
    assert gui.only("wait_text_gone") == (("處理 中", 20.0),
                                          {"region": [0, 0, 50, 50], "lang": "eng"})
    assert gui.only("wait_window_gone")[0] == ("安裝 程式", 30.0)
    assert replies.texts() == ["✅ 那段文字已經從畫面上消失", "✅ 那個視窗已經關掉了"]


@pytest.mark.parametrize("payload,reply_start", [
    ("", "用法：\n• `/locate gone text"), ("text", "用法：\n• `/locate gone text"),
    ("window", "用法：\n• `/locate gone text"), ("sound x", "用法：\n• `/locate gone text"),
    ("window 999 x", "❌ The maximum is 120 seconds."),
    ("image", "用法：上傳一張圖，訊息內容打 `/locate gone image"),
])
def test_wait_gone_refuses_bad_input(gui, replies, payload, reply_start):
    _run(b.cmd_wait_gone(_message(), payload))
    assert replies.texts()[-1].startswith(reply_start), replies
    assert gui.calls == []


# ---------------------------------------------------------------------------
# /locate pixel
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload,args,kwargs", [
    ("100 200 #2ECC71 60", (100, 200, (46, 204, 113), 60.0), {"tolerance": 12, "match": True}),
    ("100,200 not 255,0,0", (100, 200, (255, 0, 0), 30.0), {"tolerance": 12, "match": False}),
    ("1 2 != #000000 --tol 300", (1, 2, (0, 0, 0), 30.0), {"tolerance": 255, "match": False}),
    ("1 2 #000000 --tol -4", (1, 2, (0, 0, 0), 30.0), {"tolerance": 0, "match": True}),
])
def test_wait_pixel_parses_colour_negation_timeout_and_tolerance(gui, replies, payload, args,
                                                                 kwargs):
    gui.returns["wait_pixel"] = (46, 204, 113)
    _run(b.cmd_wait_pixel(_message(), payload))
    assert gui.only("wait_pixel") == (args, kwargs)
    x, y = args[:2]
    assert replies.texts() == [f"✅ ({x}, {y}) 現在是 RGB(46, 204, 113) / `#2ECC71`"]


@pytest.mark.parametrize("payload,reply_start", [
    ("1 2", "用法：`/locate pixel"), ("1 2 not", "用法：`/locate pixel"),
    ("1 2 purple", "❌ Invalid colour format"), ("1 2 #000000 --tol x", "❌ 參數不合法\n用法："),
    ("1 2 #000000 9999", "❌ The maximum is 600 seconds."), ("1 2 #000000 --tol", "❌ `--tol` 需要 1 個值。"),
])
def test_wait_pixel_refuses_bad_input_without_the_raw_error(gui, replies, payload,
                                                            reply_start):
    _run(b.cmd_wait_pixel(_message(), payload))
    reply = replies.texts()[-1]
    assert reply.startswith(reply_start), replies
    assert "invalid literal" not in reply
    assert gui.calls == []


def test_the_recording_double_covers_every_name_but_the_parsers():
    """正面對照：純解析與常數照用真的，其他任何名字——包括真模組上還沒有的——都只被記錄。
    少了這一支，替身哪天把某個桌面函式放行到真模組，上面每一支都還是綠的。"""
    fake = _RecordingGui(real_gui)
    for name in _PURE:
        assert getattr(fake, name) is getattr(real_gui, name), name
    assert fake.GuiError is real_gui.GuiError
    for name in ("mouse_click", "capture", "type_text", "load_ac"):
        assert getattr(fake, name) is not getattr(real_gui, name), name
    fake.some_future_desktop_call(1, key=2)
    assert fake.calls == [("some_future_desktop_call", (1,), {"key": 2}, "thread")]
