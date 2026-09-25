"""`_gui_control` 的純邏輯測試：參數解析、巨集存讀與驗證、指令執行。

**不碰真實的滑鼠 / 鍵盤 / 視窗。** 那些函式全部要有桌面 session 才有意義，而且
測試如果真的去點滑鼠，跑測試就會亂動使用者的畫面。這裡只測「送到底層之前」的
那一層：解析、驗證、檔案存取、逾時，以及真正需要打到系統的 `run_shell`（它的
副作用侷限在子行程裡）。

匯入走「把本檔所在目錄放進 sys.path，再直接 import `_gui_control`」這條路（與
其餘 test_*.py 一致）；`from axiomatic._gui_control import …` 只有在 pytest 把
repo root 塞進 sys.path 時才成立，單獨執行本檔會 ModuleNotFoundError。
"""
import itertools
import os
import subprocess
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _gui_control as gui  # noqa: E402
from _gui_control import GuiError  # noqa: E402


# --------------------------------------------------------------------------
# 座標 / 參數解析
# --------------------------------------------------------------------------
def test_parse_coord_accepts_negative_for_secondary_monitors():
    # 副螢幕擺在主螢幕左邊 / 上面時虛擬桌面座標是負的，不能一律當成錯誤。
    assert gui.parse_coord("-164", "y") == -164
    assert gui.parse_coord("0", "x") == 0
    assert gui.parse_coord("3455", "x") == 3455


def test_parse_coord_rejects_non_integer_and_out_of_range():
    with pytest.raises(GuiError):
        gui.parse_coord("abc", "x")
    with pytest.raises(GuiError):
        gui.parse_coord("99999999", "x")
    with pytest.raises(GuiError):
        gui.parse_coord("-99999999", "x")


def test_parse_size_rejects_zero_and_negative():
    assert gui.parse_size("10", "寬") == 10
    for bad in ("0", "-5"):
        with pytest.raises(GuiError):
            gui.parse_size(bad, "寬")


def test_parse_region_converts_xywh_to_bbox():
    assert gui.parse_region(["100", "200", "50", "40"]) == [100, 200, 150, 240]
    with pytest.raises(GuiError):
        gui.parse_region(["1", "2", "3"])


def test_parse_button_maps_aliases_and_rejects_others():
    assert gui.parse_button("r") == "mouse_right"
    assert gui.parse_button("MIDDLE") == "mouse_middle"
    assert gui.parse_button("") == "mouse_left"
    # 側鍵：很多程式把上一頁／下一頁綁在這兩顆上
    assert gui.parse_button("back") == "mouse_x1"
    assert gui.parse_button("x2") == "mouse_x2"
    with pytest.raises(GuiError):
        gui.parse_button("x3")


def test_parse_hotkey_tokens_rejects_non_alphanumeric():
    assert gui.parse_hotkey_tokens("Ctrl+Shift+T") == ["control", "shift", "t"]
    for bad in ("", "ctrl+;", "ctrl+ s/x"):
        with pytest.raises(GuiError):
            gui.parse_hotkey_tokens(bad)


def test_hotkey_aliases_map_to_the_names_the_key_table_actually_has():
    # 底層鍵名表沒有 `ctrl` / `alt` / `enter` / `esc` / `win` / `backspace`，
    # 只有 Win32 的原始名稱。少了這層對應，說明文件裡的每一個範例
    # （`!hotkey ctrl+s`、`!hotkey alt+f4`、巨集的 `hotkey enter`）都會失敗。
    assert gui.parse_hotkey_tokens("ctrl+s") == ["control", "s"]
    assert gui.parse_hotkey_tokens("alt+f4") == ["menu", "f4"]
    assert gui.parse_key_name("enter") == "return"
    assert gui.parse_key_name("esc") == "escape"
    assert gui.parse_key_name("win") == "lwin"
    assert gui.parse_key_name("backspace") == "back"


def test_parse_duration_enforces_ceiling():
    assert gui.parse_duration("2.5", maximum=10) == 2.5
    with pytest.raises(GuiError):
        gui.parse_duration("11", maximum=10)
    with pytest.raises(GuiError):
        gui.parse_duration("-1", maximum=10)


def test_split_timeout_only_treats_leading_number_as_timeout():
    assert gui.split_timeout(["10", "存檔"], default=30.0) == (10.0, "存檔")
    # 單一參數就是目標本身，不能把它吃掉當逾時
    assert gui.split_timeout(["30"], default=5.0) == (5.0, "30")
    assert gui.split_timeout(["確定", "按鈕"], default=5.0) == (5.0, "確定 按鈕")


# --------------------------------------------------------------------------
# 截圖座標空間
# --------------------------------------------------------------------------
@pytest.fixture()
def fake_grab(monkeypatch):
    """把 `ImageGrab.grab` 換成回傳**實體像素**尺寸的假圖。

    模擬本機的實際情形：邏輯虛擬桌面 3456×1244，但全桌面截圖回來是 3840×1244
    （副螢幕 125% 縮放）。這條差異是無法從程式碼本身看出來的，只能鎖在測試裡。
    """
    from PIL import Image, ImageGrab

    monkeypatch.setattr(gui, "virtual_bounds", lambda: (0, -164, 3456, 1244))

    def _grab(*_args, all_screens=False, **_kwargs):
        if all_screens:
            return Image.new("RGB", (3840, 1244), "white")
        return Image.new("RGB", (1920, 1080), "white")

    monkeypatch.setattr(ImageGrab, "grab", _grab)

    # 換算的另一半在函式庫裡，而它讀的是**真的** `GetSystemMetrics`，不是上面那個
    # `gui.virtual_bounds`。只補一半的話，這支測試等於在斷言「開發機此刻插著哪些
    # 螢幕」——2026-09-03 主機重開、外接螢幕沒接回來，全桌面那支就從
    # (3456,1244) 變成 (1920,1200) 而紅，跟程式碼一點關係都沒有。會因為環境而
    # 亂叫的測試，最後會被人當成雜訊忽略掉。
    #
    # 函式庫自己把 metrics reader 設計成可注入（docstring 明寫 "for tests"），但
    # `_gui_control.capture` 不會轉傳，所以這裡直接換掉模組層的讀取器。
    try:
        from je_auto_control.utils.monitor_layout import logical_frame
    except ImportError:      # 沒裝函式庫的直譯器：其他測試自己會 skip
        return Image
    fake_layout = {
        logical_frame.SM_XVIRTUALSCREEN: 0,
        logical_frame.SM_YVIRTUALSCREEN: -164,
        logical_frame.SM_CXVIRTUALSCREEN: 3456,
        logical_frame.SM_CYVIRTUALSCREEN: 1244,
    }
    monkeypatch.setattr(logical_frame, "_system_metrics",
                        lambda index: fake_layout.get(index, 0))
    return Image


def test_capture_all_screens_rescales_into_click_coordinate_space(
        tmp_path, fake_grab):
    # 圖上的一個像素必須等於一個點選座標，否則「照著截圖數座標」會點偏。
    dest = tmp_path / "all.png"
    gui.capture(dest, all_screens=True)
    with fake_grab.open(dest) as img:   # `with` 才會關檔；不關會留 ResourceWarning
        assert img.size == (3456, 1244)


def test_capture_primary_is_left_alone(tmp_path, fake_grab):
    # 只截主螢幕的路徑本來就是邏輯尺寸，不該被縮放動到。
    dest = tmp_path / "primary.png"
    gui.capture(dest)
    with fake_grab.open(dest) as img:
        assert img.size == (1920, 1080)


def test_capture_region_crops_in_logical_space_including_negative_origin(
        tmp_path, fake_grab):
    # 虛擬桌面從 y = -164 起算；裁切必須相對虛擬原點，不是相對 (0, 0)。
    dest = tmp_path / "region.png"
    gui.capture(dest, region=[2000, -100, 2200, 0])
    with fake_grab.open(dest) as img:
        assert img.size == (200, 100)


# --------------------------------------------------------------------------
# 巨集：解析與驗證
# --------------------------------------------------------------------------
def test_parse_macro_steps_strips_comments_blanks_and_bang_prefix():
    steps = gui.parse_macro_steps(
        "\n# 註解\n!focus chrome\n\n  hotkey ctrl+t  \ntype example.com\n")
    assert steps == ["focus chrome", "hotkey ctrl+t", "type example.com"]


def test_parse_macro_steps_rejects_empty_body():
    with pytest.raises(GuiError):
        gui.parse_macro_steps("# 只有註解\n\n")


def test_macro_cannot_smuggle_shell_execution():
    # 巨集是存在磁碟、任何人都能重播的東西；`sh` 進得去就等於繞過 `!sh` 的
    # 擁有者閘門做出一個存起來的後門。
    assert "sh" not in gui.MACRO_VERBS
    with pytest.raises(GuiError):
        gui.parse_macro_steps("sh Remove-Item -Recurse D:/")


def test_macro_step_arity_and_argument_checks():
    for bad in (
        "click 100",                 # 少一個座標
        "click 100 200 300 400",     # 太多
        "click abc 200",             # 非整數
        "click 100 200 x3",          # 不合法的滑鼠鍵（x1 / x2 側鍵是合法的）
        "drag 1 2 3",                # 拖曳需要四個座標
        "scroll 3 100",              # 座標要嘛不給、要嘛給兩個
        "hotkey ctrl+;",             # 不合法的鍵名
        "wait 9999",                 # 超過巨集的等待上限
        "win bogus chrome",          # 不存在的視窗動作
        "clip get",                  # 巨集裡只允許 clip set
        "teleport 1 2",              # 不存在的動作
    ):
        with pytest.raises(GuiError):
            gui.validate_macro_step(bad)


def test_macro_step_accepts_the_documented_forms():
    for good in (
        "click 100 200",
        "click 100 200 right",
        "dclick -50 10",
        "move 0 0",
        "drag 10 10 200 200 left",
        "scroll -3",
        "scroll -3 100 200",
        "type hello world",
        "paste 中文也可以",
        "hotkey ctrl+shift+t",
        "focus 記事本",
        "win max chrome",
        "win close chrome",
        "wait 1.5",
        "wait_window 20 記事本",
        "wait_text 20 完成",
        "click_text 確定",
        "clip set 要貼上的文字",
    ):
        gui.validate_macro_step(good)


def test_parse_macro_steps_enforces_step_ceiling():
    with pytest.raises(GuiError):
        gui.parse_macro_steps("\n".join(["wait 0"] * (gui.MACRO_MAX_STEPS + 5)))


# --------------------------------------------------------------------------
# 巨集：存 / 讀 / 列 / 刪
# --------------------------------------------------------------------------
@pytest.fixture()
def macro_dir(tmp_path, monkeypatch):
    """把巨集目錄導到 tmp_path，測試不會動到真的 `macros/`。"""
    monkeypatch.setattr(gui, "MACRO_DIR", tmp_path / "macros")
    return tmp_path / "macros"


def test_macro_name_blocks_path_traversal(macro_dir):
    del macro_dir
    for bad in ("../evil", "a/b", "with space", "", "x" * 41, "eviL!"):
        with pytest.raises(GuiError):
            gui.macro_path(bad)
    assert gui.macro_path("daily_check-2").name == "daily_check-2.json"


def test_save_then_load_roundtrip(macro_dir):
    steps = ["focus chrome", "hotkey ctrl+t", "wait 1"]
    path = gui.save_macro("daily", steps, author_id=42)
    assert path.parent == macro_dir
    data = gui.load_macro("daily")
    assert data["steps"] == steps
    assert data["author_id"] == 42
    assert data["version"] == gui.MACRO_SCHEMA_VERSION


def test_load_macro_revalidates_hand_edited_file(macro_dir):
    gui.save_macro("tampered", ["wait 1"])
    # 巨集檔在磁碟上，使用者（或別的程式）可以手動改壞它——重播前必須重驗。
    (macro_dir / "tampered.json").write_text(
        '{"version": 1, "steps": ["sh whoami"]}', encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("tampered")


def test_load_macro_rejects_corrupt_json(macro_dir):
    macro_dir.mkdir(parents=True, exist_ok=True)
    (macro_dir / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GuiError):
        gui.load_macro("broken")


def test_list_macros_skips_broken_files(macro_dir):
    gui.save_macro("good", ["wait 1", "wait 2"])
    (macro_dir / "broken.json").write_text("{nope", encoding="utf-8")
    rows = gui.list_macros()
    assert [(name, count) for name, count, _mtime in rows] == [("good", 2)]


def test_delete_macro(macro_dir):
    gui.save_macro("temp", ["wait 1"])
    gui.delete_macro("temp")
    assert not (macro_dir / "temp.json").exists()
    with pytest.raises(GuiError):
        gui.delete_macro("temp")


def test_save_macro_writes_atomically(macro_dir):
    # 原子寫入（sibling temp → os.replace）是跨行程檔案的硬性規定；寫完不該
    # 留下 .tmp 殘骸。
    gui.save_macro("atomic", ["wait 1"])
    assert not list(macro_dir.glob("*.tmp"))


def test_run_macro_stops_on_abort(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(gui, "run_macro_step",
                        lambda step, **_kw: ran.append(step) or "ok")
    calls = {"n": 0}

    def _abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    gui.run_macro(["wait 0", "wait 0", "wait 0", "wait 0"],
                  should_abort=_abort)
    assert ran == ["wait 0", "wait 0"]


def test_run_macro_treats_aborted_step_as_stop_not_failure(monkeypatch):
    # 等待步驟被中止時丟的是 GuiAborted。那是「停下來」不是「失敗」——回報成
    # 失敗的話，每次 `!macro stop` 都會在頻道上印一行紅字。
    def _raise(step, **_kw):
        raise gui.GuiAborted("停")

    monkeypatch.setattr(gui, "run_macro_step", _raise)
    assert gui.run_macro(["wait 5"]) == []


# --------------------------------------------------------------------------
# 中止感知的等待
# --------------------------------------------------------------------------
def test_sleep_abortable_returns_immediately_when_aborted():
    started = time.monotonic()
    with pytest.raises(gui.GuiAborted):
        gui._sleep_abortable(30.0, lambda: True)
    assert time.monotonic() - started < 1.0


def test_sleep_abortable_without_callback_just_sleeps():
    started = time.monotonic()
    gui._sleep_abortable(0.05, None)
    assert time.monotonic() - started >= 0.04


def test_wait_text_aborts_without_running_to_timeout(monkeypatch):
    # 沒有中止回呼的話，`!macro stop` 對一個 `wait_text 120 …` 要等兩分鐘才生效。
    monkeypatch.setattr(gui, "find_text", lambda *a, **k: [])
    started = time.monotonic()
    with pytest.raises(gui.GuiAborted):
        gui.wait_text("完成", 120.0, should_abort=lambda: True)
    assert time.monotonic() - started < 1.0


def test_wait_pixel_aborts_without_running_to_timeout(monkeypatch):
    monkeypatch.setattr(gui, "pixel_color", lambda x, y: (0, 0, 0))
    started = time.monotonic()
    with pytest.raises(gui.GuiAborted):
        gui.wait_pixel(1, 1, (255, 255, 255), 120.0, should_abort=lambda: True)
    assert time.monotonic() - started < 1.0


def test_gui_aborted_is_still_a_gui_error():
    # 不認得這個型別的呼叫端照樣接得到、照樣拿得到一句安全的訊息。
    assert issubclass(gui.GuiAborted, GuiError)


# --------------------------------------------------------------------------
# 打字（標點與非 ASCII）
# --------------------------------------------------------------------------
def test_type_text_delegates_to_the_backend(monkeypatch):
    # 打字的實作在桌面自動化函式庫裡，本專案不重做一份。
    written: list[str] = []

    class _Backend:
        @staticmethod
        def write(text):
            written.append(text)

    monkeypatch.setattr(gui, "input_desktop_available", lambda: True)
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    gui.type_text("https://a.b/c?d=1 中文，測試")
    assert written == ["https://a.b/c?d=1 中文，測試"]


def test_type_text_reports_a_generic_error_on_backend_failure(monkeypatch):
    class _Backend:
        @staticmethod
        def write(text):
            raise KeyError(text)      # 鍵名表查不到就是這個形狀

    monkeypatch.setattr(gui, "input_desktop_available", lambda: True)
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    # 哨符刻意選一個**不可能出現在正常中文訊息裡**的字串。原本用的是「，」，
    # 而那是一般標點：2026-09-05 給失敗訊息加上「已放開 N 個按鍵」之後，訊息裡
    # 合法的頓號就讓這支測試紅了——守的東西是對的（不得回吐輸入／原始例外），
    # 判準太寬而已。換成哨符之後兩者都成立。
    sentinel = "⟪TYPED-INPUT-SENTINEL⟫"
    with pytest.raises(GuiError) as caught:
        gui.type_text(sentinel)
    # 訊息必須是本模組寫死的泛用字串，不能夾帶輸入內容或原始例外文字
    message = str(caught.value)
    assert sentinel not in message
    assert "KeyError" not in message


def test_backend_write_covers_punctuation_and_cjk():
    """底層真的打得出標點與中日文（這是 `!type` 曾經整句失敗的原因）。

    只驗證**決策**，不真的送出去：實際送出會打進當下的焦點視窗。
    """
    try:
        backend = gui.load_ac()
    except GuiError:
        pytest.skip("桌面自動化後端在此環境不可用")
    assert backend.unicode_keys_supported() is True
    plan = backend.plan_unicode_keys("a,:/?中")
    assert [unit["unit"] for unit in plan] == [
        ord("a"), ord(","), ord(":"), ord("/"), ord("?"), ord("中")]
    # BMP 以外的字元在 UTF-16 下是代理對，要各送一筆
    assert [unit["unit"] for unit in backend.plan_unicode_keys("\U0001F600")] == [
        0xD83D, 0xDE00]


# --------------------------------------------------------------------------
# UI 元素定位：轉呼叫函式庫，本模組只做型別驗證與欄位轉換
# --------------------------------------------------------------------------
class _FakeElement:
    """函式庫 `AccessibilityElement` 的替身。"""

    def __init__(self, name, role, bounds, enabled=True):
        self.name = name
        self.role = role
        self.bounds = bounds
        self.enabled = enabled

    @property
    def center(self):
        left, top, width, height = self.bounds
        return (left + width // 2, top + height // 2)


class _FakeUiBackend:
    """記下傳給函式庫的參數，好驗證視窗片段與型別有正確送過去。"""

    calls: list = []
    elements: list = []
    state: dict = {}

    @classmethod
    def find_accessibility_elements(cls, **kwargs):
        cls.calls.append(("find", kwargs))
        return list(cls.elements)

    @classmethod
    def list_accessibility_elements(cls, **kwargs):
        cls.calls.append(("list", kwargs))
        return list(cls.elements)

    @classmethod
    def control_get_state(cls, **kwargs):
        cls.calls.append(("state", kwargs))
        return dict(cls.state)

    @staticmethod
    def humanize_role(role):
        return {"ControlType_50000": "Button",
                "ControlType_50004": "Edit"}.get(role, role)


def _stub_ui(monkeypatch, elements=(), state=None):
    _FakeUiBackend.calls = []
    _FakeUiBackend.elements = list(elements)
    _FakeUiBackend.state = dict(state or {})
    monkeypatch.setattr(gui, "load_ac", lambda: _FakeUiBackend)
    return _FakeUiBackend


def test_ui_find_maps_fields_and_humanises_the_type(monkeypatch):
    # 函式庫刻意保留原始型別代碼（翻譯是獨立一步），但回給使用者要看得懂。
    backend = _stub_ui(monkeypatch, elements=[
        _FakeElement("確定", "ControlType_50000", (10, 20, 80, 30))])
    rows = gui.ui_find("確定", control_type="button", window="記事本 - 未命名")
    assert rows == [{"name": "確定", "type": "button", "x": 50, "y": 35,
                     "left": 10, "top": 20, "width": 80, "height": 30,
                     "enabled": True}]
    kind, kwargs = backend.calls[0]
    assert kind == "find"
    assert kwargs["window_title"] == "記事本 - 未命名"
    assert kwargs["role"] == "button"
    assert kwargs["contains"] is True          # 名稱常帶快捷鍵標記與後綴


def test_ui_find_separates_match_cap_from_scan_cap(monkeypatch):
    # 把命中上限拿去當掃描上限，等於「只看畫面上前 40 個元素」——幾乎什麼都
    # 找不到，而且看起來像「這個元素不支援這種定位」。
    backend = _stub_ui(monkeypatch, elements=[])
    gui.ui_find("確定")
    kwargs = backend.calls[0][1]
    assert kwargs["max_results"] == gui.UI_MAX_RESULTS
    assert kwargs["scan_limit"] == gui.UI_SCAN_LIMIT
    assert gui.UI_SCAN_LIMIT > gui.UI_MAX_RESULTS


def test_ui_find_exact_turns_off_substring_matching(monkeypatch):
    backend = _stub_ui(monkeypatch, elements=[])
    gui.ui_find("確定", exact=True)
    assert backend.calls[0][1]["contains"] is False


def test_ui_find_drops_zero_area_elements(monkeypatch):
    # 沒有實際版面的元素點不到，列出來只會誤導人。
    _stub_ui(monkeypatch, elements=[
        _FakeElement("看不到", "ControlType_50000", (0, 0, 0, 0)),
        _FakeElement("看得到", "ControlType_50000", (0, 0, 10, 10))])
    assert [row["name"] for row in gui.ui_find("看")] == ["看得到"]


def test_ui_find_rejects_an_unknown_type(monkeypatch):
    _stub_ui(monkeypatch)
    with pytest.raises(GuiError):
        gui.ui_find("確定", control_type="nope")


def test_ui_find_requires_a_name(monkeypatch):
    _stub_ui(monkeypatch)
    with pytest.raises(GuiError):
        gui.ui_find("   ")


def test_ui_value_scopes_the_state_read_to_the_window(monkeypatch):
    # 讀值要再走一次樹；不限定視窗的話那一步是整個桌面（本機實測數十秒）。
    backend = _stub_ui(
        monkeypatch,
        elements=[_FakeElement("網址列", "ControlType_50004", (0, 0, 100, 20))],
        state={"value": "https://example.test", "read_only": False})
    rows = gui.ui_value("網址", window="瀏覽器")
    assert rows[0]["value"] == "https://example.test"
    assert rows[0]["type"] == "edit"
    state_call = next(kw for kind, kw in backend.calls if kind == "state")
    assert state_call["window_title"] == "瀏覽器"


def test_ui_value_reports_a_password_field_without_its_content(monkeypatch):
    _stub_ui(monkeypatch,
             elements=[_FakeElement("密碼", "ControlType_50004", (0, 0, 90, 20))],
             state={"password": True})
    row = gui.ui_value("密碼")[0]
    assert row["password"] is True and "value" not in row


def test_ui_missing_window_is_reported_as_such(monkeypatch):
    class _Missing:
        @staticmethod
        def list_accessibility_elements(**_kwargs):
            raise _fake_a11y_error("no visible window title contains 'zzz'")

    monkeypatch.setattr(gui, "load_ac", lambda: _Missing)
    with pytest.raises(GuiError) as caught:
        gui.ui_tree("zzz")
    # 「找不到視窗」是使用者改得了的，不能折成「這台機器沒有這個功能」
    assert "window" in str(caught.value)


def _fake_a11y_error(message):
    """名字要跟函式庫的一致——`_ui_call` 是照型別名稱分流的。"""
    return type("AccessibilityNotAvailableError", (RuntimeError,), {})(message)


# --------------------------------------------------------------------------
# 指令執行
# --------------------------------------------------------------------------
def test_shell_argv_is_non_interactive():
    argv = gui.shell_argv("echo hi")
    # Windows 上使用者的指令前面接了 UTF-8 前綴（見 `_PS_UTF8_PRELUDE`），所以
    # 這裡問的是「指令有沒有原封不動留在最後」，不是「整格等於指令」。
    assert argv[-1].endswith("echo hi")
    if sys.platform.startswith("win"):
        # 載入 profile 會拖慢每次呼叫又可能改動環境；互動提示在沒有 tty 的
        # 子行程裡只會卡到逾時。
        assert "-NoProfile" in argv and "-NonInteractive" in argv
    else:
        assert argv[0] == "/bin/sh"


def test_run_shell_rejects_empty_command():
    with pytest.raises(GuiError):
        gui.run_shell("   ")


def test_run_shell_returns_exit_code_and_output():
    result = gui.run_shell("echo marker-value", timeout=30)
    assert result["rc"] == 0
    assert "marker-value" in result["output"]
    assert result["timed_out"] is False


def test_run_shell_propagates_non_zero_exit():
    # 兩個平台剛好同一句：PowerShell 與 sh 都認得 `exit 3`。原本寫成三元運算子，
    # 兩邊卻是一樣的字串，看起來像「有處理平台差異」其實沒有。
    command = "exit 3"
    result = gui.run_shell(command, timeout=30)
    assert result["rc"] == 3


def test_run_shell_times_out_and_kills_the_tree():
    sleep_cmd = ("Start-Sleep -Seconds 30" if sys.platform.startswith("win")
                 else "sleep 30")
    started = time.monotonic()
    result = gui.run_shell(sleep_cmd, timeout=2)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    # 逾時要真的中止；只殺直接子行程而不殺孫行程時這裡會拖到 30 秒。
    assert elapsed < 15


def test_strip_ansi_removes_terminal_colour_codes():
    # PowerShell 7 的 formatter 會在表格輸出裡夾顏色碼；那在對話平台上是亂碼。
    assert gui.strip_ansi("\x1b[32;1mPath\x1b[0m") == "Path"
    assert gui.strip_ansi("\x1b]0;title\x07body") == "body"
    assert gui.strip_ansi("plain") == "plain"


def test_run_shell_output_has_no_ansi_codes():
    result = gui.run_shell("Get-Location", timeout=30)
    assert "\x1b[" not in result["output"]


def test_run_shell_truncates_huge_output(monkeypatch):
    monkeypatch.setattr(gui, "SHELL_MAX_OUTPUT_CHARS", 100)
    command = ("1..500 | ForEach-Object { 'xxxxxxxxxx' }"
               if sys.platform.startswith("win")
               else "for i in $(seq 500); do echo xxxxxxxxxx; done")
    result = gui.run_shell(command, timeout=60)
    assert "truncated" in result["output"]
    assert len(result["output"]) < 200


def test_run_shell_clamps_timeout_to_ceiling(monkeypatch):
    seen = {}

    class _FakeProc:
        returncode = 0
        pid = -1

        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            return "", ""

    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: _FakeProc())
    gui.run_shell("echo hi", timeout=99_999)
    assert seen["timeout"] == gui.SHELL_MAX_TIMEOUT_SEC


# --------------------------------------------------------------------------
# OCR 可用性
# --------------------------------------------------------------------------
def test_ocr_status_returns_generic_reason(monkeypatch):
    ok, reason = gui.ocr_status()
    assert isinstance(ok, bool)
    # 訊息會直接回給使用者，必須是本模組寫死的泛用字串（不含路徑 / 原始例外）。
    assert reason and ":" not in reason and "\\" not in reason


def test_text_commands_report_unavailable_ocr_instead_of_raising(monkeypatch):
    class OCRBackendNotAvailableError(RuntimeError):
        """名字要跟函式庫的一致——`_ocr_call` 是照型別名稱分流的。"""

    class _Backend:
        @staticmethod
        def find_text_matches(*_args, **_kwargs):
            raise OCRBackendNotAvailableError("no engine")

        @staticmethod
        def set_tesseract_cmd(_path):
            pass

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    monkeypatch.setattr(gui, "ocr_lang_for", lambda target, lang=None: "eng")
    monkeypatch.setattr(gui, "ocr_status",
                        lambda: (False, "Text recognition is disabled: test."))
    for call in (lambda: gui.find_text("x"),
                 lambda: gui.click_text("x"),
                 lambda: gui.wait_text("x", 1)):
        with pytest.raises(GuiError) as excinfo:
            call()
        # 「引擎沒裝」是使用者補得了的，不能被折成泛用的「辨識失敗」
        assert "Text recognition is disabled" in str(excinfo.value)


# --------------------------------------------------------------------------
# OCR 探測：呼叫方向、失敗理由、晚一步安裝
# --------------------------------------------------------------------------
_FAKE_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _fake_pytesseract(*, version_error=None, langs=("eng",), langs_error=None):
    """`pytesseract` 的替身，讓這一族測試不依賴主機上到底有沒有裝引擎。

    ⚠️ **`get_tesseract_version()` 必須在沒有可用路徑時丟例外。** 真的那一支丟
    `TesseractNotFoundError`（`OSError` 的子類）。替身若不忠實地永遠回一個版本號，
    「引擎沒裝」那一輪就會**成功**、`_OCR` 被設起來，於是測試根本走不到缺陷需要的
    狀態——而且看起來是綠的。實測過：不忠實的替身讓其中兩支測試在**未修正**的程式
    碼上照樣通過。
    """
    import types

    mod = types.ModuleType("pytesseract")
    mod.pytesseract = types.SimpleNamespace(tesseract_cmd=None)

    def get_tesseract_version():
        if version_error is not None:
            raise version_error
        if not mod.pytesseract.tesseract_cmd:
            raise OSError("tesseract is not installed or it's not in your PATH")
        return "5.3.0"

    def get_languages(config=""):
        if langs_error is not None:
            raise langs_error
        return list(langs)

    mod.get_tesseract_version = get_tesseract_version
    mod.get_languages = get_languages
    return mod


def _ocr_env(monkeypatch, *, cmd, pt):
    """把 OCR 那一段的模組層狀態設成乾淨值，回傳「函式庫被告知的路徑」清單。

    全部走 `monkeypatch.setattr`，所以測試結束會自動還原——這些是行程層級的閂，
    漏還原會把「快取起來的失敗」洩漏給後面每一支測試。
    """
    import types

    monkeypatch.setattr(gui, "_OCR", None)
    monkeypatch.setattr(gui, "_OCR_TRIED", False)
    monkeypatch.setattr(gui, "_OCR_CONFIGURED", False)
    monkeypatch.setattr(gui, "_OCR_REASON", "文字辨識未啟用：辨識引擎無法使用。")
    monkeypatch.setattr(gui, "_OCR_PROBED_CMD", None)
    monkeypatch.setattr(gui, "tesseract_cmd",
                        cmd if callable(cmd) else (lambda: cmd))
    monkeypatch.setitem(sys.modules, "pytesseract", pt)
    told: list[str] = []
    monkeypatch.setattr(
        gui, "load_ac",
        lambda: types.SimpleNamespace(set_tesseract_cmd=told.append))
    return told


def test_ocr_status_does_not_recurse_into_load_ocr(monkeypatch):
    """引擎路徑找得到但跑不起來 → 回那句具體的話，不是 `RecursionError`。

    這一格在 2026-09-11 之前是**死碼**：唯一到得了它的路徑是
    `_load_ocr()` 丟 `GuiError(ocr_status()[1])`，而例外的引數在 raise 之前就求值，
    所以 `ocr_status` 外面那個 `except GuiError` 永遠等不到——先撞 `RecursionError`
    （實測 1000 層），而它不是 `GuiError`，會一路竄出去。
    """
    _ocr_env(monkeypatch, cmd=_FAKE_TESSERACT,
             pt=_fake_pytesseract(version_error=OSError("bad binary")))
    assert gui.ocr_status() == (
        False, "Text recognition is disabled: the recognition engine executable cannot run.")


def test_load_ocr_reports_the_engine_reason_itself(monkeypatch):
    """探測失敗時 `_load_ocr` 自己說得出理由，不必回頭問 `ocr_status`。"""
    _ocr_env(monkeypatch, cmd=_FAKE_TESSERACT,
             pt=_fake_pytesseract(version_error=OSError("bad binary")))
    with pytest.raises(GuiError) as excinfo:
        gui._load_ocr()
    assert "cannot run" in str(excinfo.value)


def test_load_ocr_never_reaches_ocr_status():
    """AST：`_load_ocr` 走不回 `ocr_status`。**只有這一支抓得到那個回歸。**

    把 `_load_ocr` 最終那個 raise 改回 `ocr_status()[1]`，行為測試會**全綠**——
    因為「快取起來的失敗」那一支會把環擋在深度 2，於是它自己收斂了。行為完全正確，
    可是那條反向邊已經回來，等哪天有人再動快取邏輯，無界遞迴就復活。變異實測驗證
    過這一點：整支檔案跑 → 紅；**扣掉這一支** → 354 passed、全綠；只跑這一支 → 紅。

    ⚠️ **這支跟 `test_bot_helpers.test_no_raise_argument_can_re_enter_the_function_that_raises`
    不是重複的，兩支都要留。** 那一支是跨專案的規則，只管「`raise X(...)` 的**引數**
    裡的呼叫」；這一支管的是更強的**本地**不變量——`_load_ocr` 不得以**任何**形式
    走到 `ocr_status`，因為就算不在 raise 的引數裡，那個環一樣會遞迴。刪掉其中任何
    一支都會留下一個真的洞。
    """
    import ast
    from pathlib import Path

    def _reachable(tree, start):
        fns = {n.name: n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        graph = {name: {s.func.id for s in ast.walk(node)
                        if isinstance(s, ast.Call)
                        and isinstance(s.func, ast.Name)
                        and s.func.id in fns}
                 for name, node in fns.items()}
        seen, stack = set(), [start]
        while stack:
            for nxt in graph.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    # 正面對照組：正方向必須真的存在，否則抽取器回空集合時這支會空轉通過。
    assert "_load_ocr" in _reachable(tree, "ocr_status"), "抽取器什麼都沒抽到"
    assert "ocr_status" not in _reachable(tree, "_load_ocr"), (
        "`_load_ocr` 又走得回 `ocr_status` 了——raise 的引數會無限遞迴")


def test_a_cached_failure_is_re_probed_when_the_engine_appears(monkeypatch):
    """沒裝引擎 → 下過一次文字指令 → 把引擎裝好 → 這個長命行程要看得見。

    這是那個遞迴**最容易被觸發**的路徑，不需要壞掉的安裝或錯的環境變數。
    """
    state = {"cmd": None}
    _ocr_env(monkeypatch, cmd=lambda: state["cmd"], pt=_fake_pytesseract())
    assert gui.ocr_status() == (
        False, "Text recognition is disabled: the recognition engine executable is not installed.")
    # ⚠️ 這一步不能省：`/locate text find` 走的是 `ocr_lang_for`，而**舊碼在沒有
    # 引擎時根本不會呼叫 `_load_ocr`**（`ocr_status` 第二道檢查就 return 了），
    # 所以少了它，`_OCR_TRIED` 從沒被設起來，這支測試在舊碼上照樣是綠的。
    assert gui.ocr_lang_for("存檔") == "eng"
    assert gui._OCR_TRIED is True
    state["cmd"] = _FAKE_TESSERACT
    ok, reason = gui.ocr_status()
    assert ok, f"引擎裝好了卻還在報：{reason}"


def test_the_library_learns_the_path_after_a_late_install(monkeypatch):
    """晚一步安裝之後，函式庫要真的被告知執行檔路徑。

    實際的辨識是走函式庫（`_ocr_call`），不是走本模組的 `_OCR`。`_OCR_CONFIGURED`
    原本無條件設在 `_configure_ocr` 開頭，所以這條路上函式庫**永遠**拿不到路徑：
    `ocr_status()` 說可用、每個辨識指令卻回泛用的「文字辨識失敗。」。修好遞迴而不
    修這裡的話，只是把崩潰換成一個更難查的靜默失敗。
    """
    state = {"cmd": None}
    told = _ocr_env(monkeypatch, cmd=lambda: state["cmd"],
                    pt=_fake_pytesseract())
    gui.ocr_lang_for("存檔")
    assert told == [], "還沒有引擎就不該告訴函式庫任何路徑"
    state["cmd"] = _FAKE_TESSERACT
    assert gui.ocr_status()[0]
    assert told == [_FAKE_TESSERACT], told


def test_the_success_path_does_not_rescan_path(monkeypatch):
    """成功之後 `_load_ocr` 要早退，不得再掃 PATH。

    `wait_text` 每 0.5 秒輪詢一次 `find_text` → `ocr_lang_for` → `ocr_languages`
    → `_load_ocr`，而 `tesseract_cmd()` 會走 `shutil.which`（本機實測 4.0 ms，掃
    39 個 PATH 項目）。早退版實測 0.0001 ms。
    """
    seen: list[int] = []

    def _cmd():
        seen.append(1)
        return _FAKE_TESSERACT

    _ocr_env(monkeypatch, cmd=_cmd, pt=_fake_pytesseract())
    gui._load_ocr()
    before = len(seen)
    gui._load_ocr()
    gui._load_ocr()
    assert len(seen) == before, "成功之後還在掃 PATH"


def test_ocr_status_reports_missing_language_data(monkeypatch):
    """引擎答了、清單真的是空的 → 整個功能不可用，而且要指出是缺哪一塊。"""
    _ocr_env(monkeypatch, cmd=_FAKE_TESSERACT,
             pt=_fake_pytesseract(langs=()))
    assert gui.ocr_status() == (False, "Text recognition is disabled: language recognition data is missing.")


def test_an_unaskable_language_list_is_not_reported_as_unavailable(monkeypatch):
    """列舉本身失敗（問不到）**不算**沒有語言資料——方向要跟 `ocr_lang_for` 一致。

    那半的成因幾乎都是引擎載不起來，而那件事由前面的探測報得更準；為一次列舉打嗝
    就宣告整個功能不可用，正是 `ocr_lang_for` 的白名單刻意不做的事。
    """
    _ocr_env(monkeypatch, cmd=_FAKE_TESSERACT,
             pt=_fake_pytesseract(langs_error=RuntimeError("enumerate blew up")))
    assert gui.ocr_status() == (True, "Text recognition is available.")


def test_ocr_status_and_ocr_lang_for_agree_about_zero_language_data(monkeypatch):
    """對帳：同一份 `(langs, known)` 餵給兩個呼叫端，答案必須一致。

    兩邊是**語意耦合、不是程式碼共用**（`ocr_lang_for` 的白名單更寬，擋不進
    `_ocr_no_language_data`），所以其中一邊改掉不會有任何症狀——只有這支會變紅。
    """
    corpus = [(["chi_tra", "eng"], True), (["eng"], True),
              ([], True), ([], False)]
    rows = []
    for langs, known in corpus:
        monkeypatch.setattr(gui, "ocr_languages",
                            lambda _l=langs, _k=known: (_l, _k))
        monkeypatch.setattr(gui, "_load_ocr", lambda: object())
        ok, reason = gui.ocr_status()
        status_blames = (not ok) and reason == (
            "Text recognition is disabled: language recognition data is missing.")
        try:
            gui.ocr_lang_for("x", langs[0] if langs else "eng")
            lang_for_blocks = False
        except GuiError:
            lang_for_blocks = True
        assert status_blames == lang_for_blocks, (
            f"{langs} / known={known}：ocr_status 說 {status_blames}，"
            f"ocr_lang_for 說 {lang_for_blocks}")
        rows.append(status_blames)
    # 正面對照組：語料真的咬得到——兩種答案都要出現，否則這支在任何實作上都綠。
    assert any(rows) and not all(rows), rows


# --------------------------------------------------------------------------
# 辨識語言的自動判斷
# --------------------------------------------------------------------------
class _FakeOcrLangs:
    """`_load_ocr()` 的替身，用來控制「已安裝的語言」這個答案。

    ⚠️ **要換掉的是 `_load_ocr`，不是 `ocr_languages`。** 換掉後者會讓「那個旗標是
    在哪裡被算出來的」這道接縫整個看不見：把 `except: return [], False` 寫成
    `return [], True`（把「問不到」誤標成「問到了、是空的」）——一個會讓沒裝引擎的
    機器上每一個辨識指令都硬失敗的回歸——在只換 `ocr_languages` 的測試底下**全綠**。

    ⚠️ 這個替身還有第二個作用：簽章裡**沒有** `cached` 參數。哪天有人為了「省一次
    子行程」加上 `get_languages(config="", cached=True)`，這裡會當場 `TypeError`，
    而不是安靜地把一次早期的空清單快取成整個行程的答案。

    ⚠️ 順帶記一個踩過的坑：`monkeypatch.setattr(gui, "ocr_languages",
    lambda: ["chi_tra", "eng"])` 這種**兩元素 list** 的替身，會被
    `langs, known = ocr_languages()` **靜靜解包**成兩個字串（`langs="chi_tra"`、
    `known="eng"`），不會報錯，只會讓後面的成員檢查全部答錯。
    """

    def __init__(self, langs):
        self._langs = langs

    def get_languages(self, config=""):
        assert config == ""
        return list(self._langs)


def _ocr_unavailable():
    """`_load_ocr()` 問不到的情形——正式碼裡最常見的成因是引擎根本沒裝。"""
    raise GuiError("文字辨識未啟用：測試。")


def test_ocr_languages_separates_cannot_ask_from_none_installed(monkeypatch):
    """「問不到」與「真的一個都沒裝」以前都是空清單，於是呼叫端沒辦法分開處置。"""
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs(["eng", "chi_tra"]))
    assert gui.ocr_languages() == (["chi_tra", "eng"], True)
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs([]))
    assert gui.ocr_languages() == ([], True)
    monkeypatch.setattr(gui, "_load_ocr", _ocr_unavailable)
    assert gui.ocr_languages() == ([], False)


def test_ocr_lang_for_lets_an_override_through_when_the_list_cannot_be_read(
        monkeypatch):
    """問不到 ≠ 沒安裝。擋下來只會把「引擎沒裝」報成「語言沒裝」，指錯那一半。"""
    monkeypatch.setattr(gui, "_load_ocr", _ocr_unavailable)
    assert gui.ocr_lang_for("x", "jpn") == "jpn"
    assert gui.ocr_lang_for("x", "chi_tra+eng") == "chi_tra+eng"
    # 放行只限白名單那一層；格式檢查照舊擋。
    with pytest.raises(GuiError):
        gui.ocr_lang_for("x", "../etc")


def test_ocr_lang_for_blocks_when_the_engine_says_zero_languages(monkeypatch):
    """引擎答了、清單真的是空的 → 擋，並印出「已安裝：（無）」。

    這個分支在 2026-09-11 之前是**死碼**：空清單時 `missing` 也一定是空的，
    `or "（無）"` 那一支永遠走不到。所以這裡要驗它真的印得出來。
    """
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs([]))
    with pytest.raises(GuiError) as excinfo:
        gui.ocr_lang_for("x", "jpn")
    assert "(none)" in str(excinfo.value)


def test_ocr_lang_for_auto_pick_is_unchanged_when_there_is_no_list(monkeypatch):
    """自動選語言那一段沒有白名單語意——「不知道有什麼就退回 eng」本來就是它的
    政策，不是被跳過的檢查。兩種空清單都必須維持今天的行為。"""
    for stub in (lambda: _FakeOcrLangs([]), _ocr_unavailable):
        monkeypatch.setattr(gui, "_load_ocr", stub)
        assert gui.ocr_lang_for("確定") == "eng"
        assert gui.ocr_lang_for("") == "eng"


def test_ocr_lang_for_picks_chinese_model_when_target_has_cjk(monkeypatch):
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs(["chi_tra", "eng"]))
    # 要找中文卻用英文模型辨識 = 永遠找不到而且沒有線索，所以自動掛上中文模型
    assert gui.ocr_lang_for("確定") == "chi_tra+eng"
    assert gui.ocr_lang_for("Save") == "eng"


def test_ocr_lang_for_validates_explicit_override(monkeypatch):
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs(["chi_tra", "eng"]))
    assert gui.ocr_lang_for("確定", "eng") == "eng"
    assert gui.ocr_lang_for("x", "chi_tra+eng") == "chi_tra+eng"
    with pytest.raises(GuiError):
        gui.ocr_lang_for("x", "jpn")          # 沒安裝
    with pytest.raises(GuiError):
        gui.ocr_lang_for("x", "../etc")       # 不是合法代碼


# --------------------------------------------------------------------------
# 文字定位：轉呼叫函式庫，本模組只負責選語言與欄位轉換
# --------------------------------------------------------------------------
class _FakeMatch:
    """函式庫 `TextMatch` 的替身（只用到 text / center / confidence）。"""

    def __init__(self, text, x, y, confidence=90.0):
        self.text = text
        self.center = (x, y)
        self.confidence = confidence


class _FakeOcrBackend:
    """記下傳給函式庫的參數，好驗證區域與語言有正確送過去。"""

    calls: list = []
    matches: list = []
    words: list = []

    @classmethod
    def find_text_matches(cls, target, lang, region, min_confidence,
                          case_sensitive):
        cls.calls.append({"target": target, "lang": lang, "region": region,
                          "min_confidence": min_confidence,
                          "case_sensitive": case_sensitive})
        return list(cls.matches)

    @classmethod
    def read_text_in_region(cls, region=None, lang="eng", min_confidence=60.0):
        cls.calls.append({"region": region, "lang": lang,
                          "min_confidence": min_confidence})
        return list(cls.words)

    @staticmethod
    def group_lines(words):
        return [list(words)] if words else []

    @staticmethod
    def set_tesseract_cmd(_path):
        pass


def _stub_backend(monkeypatch, *, matches=(), words=()):
    _FakeOcrBackend.calls = []
    _FakeOcrBackend.matches = list(matches)
    _FakeOcrBackend.words = list(words)
    monkeypatch.setattr(gui, "load_ac", lambda: _FakeOcrBackend)
    monkeypatch.setattr(gui, "ocr_lang_for", lambda target, lang=None: "chi_tra")
    return _FakeOcrBackend


def test_find_text_delegates_and_maps_fields(monkeypatch):
    # 跨詞比對與座標換算都在函式庫裡（那裡有測試）；本模組負責的是選語言、
    # 把 bbox 轉成函式庫要的 x/y/寬/高，以及回傳呼叫端用的欄位名。
    backend = _stub_backend(monkeypatch, matches=[
        _FakeMatch("另存 新檔", 120, 205, 88.0)])
    hits = gui.find_text("另存新檔", region=[100, 200, 700, 600])
    assert hits == [{"text": "另存 新檔", "x": 120, "y": 205, "confidence": 88.0}]
    assert backend.calls[0]["lang"] == "chi_tra"
    assert backend.calls[0]["region"] == (100, 200, 600, 400)


def test_find_text_sorts_hits_in_reading_order(monkeypatch):
    _stub_backend(monkeypatch, matches=[
        _FakeMatch("b", 300, 400), _FakeMatch("a", 10, 100)])
    assert [hit["text"] for hit in gui.find_text("x")] == ["a", "b"]


def test_find_text_rejects_an_empty_target(monkeypatch):
    _stub_backend(monkeypatch)
    with pytest.raises(GuiError):
        gui.find_text("   ")


def test_read_text_groups_words_into_lines(monkeypatch):
    _stub_backend(monkeypatch, words=[
        _FakeMatch("Save", 20, 100, 95.0), _FakeMatch("As", 60, 100, 70.0)])
    rows = gui.read_text()
    # 一行的信心度取最弱的那個詞——那才是整行讀對的機率
    assert rows == [{"text": "Save As", "x": 40, "y": 100, "confidence": 70.0}]


def test_find_text_passes_the_confidence_floor_through(monkeypatch):
    # 低信心度的辨識結果由函式庫在後端就濾掉（那裡有測試）；這裡只確認門檻真的
    # 有送過去，不會因為本模組漏傳而變成預設值。
    backend = _stub_backend(monkeypatch)
    assert gui.find_text("確定", min_confidence=75.0) == []
    assert backend.calls[0]["min_confidence"] == 75.0


# --------------------------------------------------------------------------
# 圖片定位：座標必須落在「點選座標」那個空間
# --------------------------------------------------------------------------
def _fake_desktop(monkeypatch, origin=(-100, -50), size=(400, 300)):
    """造一張固定的假桌面（雜訊底），並把虛擬桌面原點設成非 (0, 0)。

    攔的是**函式庫的擷取入口**（比對本身在那邊做），不是本模組——本模組已經不再
    自己截圖與比對。
    """
    Image = pytest.importorskip("PIL.Image", reason="需要 Pillow")
    numpy = pytest.importorskip("numpy", reason="需要 numpy")
    visual_match = pytest.importorskip(
        "je_auto_control.utils.visual_match.visual_match", reason="需要函式庫")
    import random
    random.seed(7)
    frame = Image.new("RGB", size)
    frame.putdata([(random.randrange(256), random.randrange(256),
                    random.randrange(256)) for _ in range(size[0] * size[1])])
    gray = numpy.asarray(frame.convert("L"))
    monkeypatch.setattr(visual_match, "_grab_gray_with_origin",
                        lambda region: (gray, origin[0], origin[1]))
    return frame


def test_locate_image_returns_click_space_coordinates(monkeypatch, tmp_path):
    # 這是這個功能唯一的重點：圖上的一個像素要等於一個點選座標。函式庫負責把
    # 虛擬桌面原點加回去（它曾經漏掉，也曾經只截主螢幕），這裡驗它真的有加。
    pytest.importorskip("cv2", reason="需要 cv2")
    frame = _fake_desktop(monkeypatch)
    template = tmp_path / "tpl.png"
    frame.crop((250, 180, 280, 200)).save(str(template))
    assert gui.locate_image(str(template), threshold=0.99) == (
        -100 + 250 + 15, -50 + 180 + 10)


def test_locate_image_rejects_flat_template(monkeypatch, tmp_path):
    # 單色樣板會讓正規化相關係數退化（分母是樣板變異數），整片畫面都回 1.0 然後
    # 「找到了」並點在隨機位置——比直接失敗糟糕得多。
    pytest.importorskip("cv2", reason="需要 cv2")
    Image = pytest.importorskip("PIL.Image", reason="需要 Pillow")
    _fake_desktop(monkeypatch)
    template = tmp_path / "flat.png"
    Image.new("RGB", (20, 20), (12, 34, 56)).save(str(template))
    with pytest.raises(GuiError) as excinfo:
        gui.locate_image(str(template))
    assert "single colour" in str(excinfo.value)


def test_locate_image_reports_missing_template_distinctly(monkeypatch, tmp_path):
    pytest.importorskip("cv2", reason="需要 cv2")
    _fake_desktop(monkeypatch)
    with pytest.raises(GuiError) as excinfo:
        gui.locate_image(str(tmp_path / "nope.png"))
    assert "Failed to read" in str(excinfo.value)


_LEAKY = RuntimeError(r"C:\Users\someone\AppData\engine\core.dll: access violation")


class _FailingBackend:
    """每個入口都丟一個訊息裡帶主機路徑的例外——也就是函式庫真的會丟的那種。"""

    def __init__(self, error=_LEAKY, hits=None):
        self.error, self.hits = error, hits

    def match_template_all(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.hits or []

    def read_text_in_region(self, *_args, **_kwargs):
        raise self.error

    @staticmethod
    def group_lines(words):
        return words


def _generic_only(excinfo, expected: str) -> None:
    """`GuiError` 的文字會**照原樣**送到聊天室（`_SAFE_EXCEPTION_TYPES` 的豁免），所以
    被折起來的原始例外連一個字都不能漏進去。"""
    assert str(excinfo.value) == expected
    assert "someone" not in str(excinfo.value) and "dll" not in str(excinfo.value)


def test_an_unexpected_match_failure_reaches_the_user_as_a_generic_line(
        monkeypatch):
    monkeypatch.setattr(gui, "load_ac", lambda: _FailingBackend())
    with pytest.raises(GuiError) as excinfo:
        gui.locate_image("tpl.png")
    _generic_only(excinfo, "Image matching failed.")


def test_an_unexpected_ocr_failure_reaches_the_user_as_a_generic_line(
        monkeypatch):
    """與「引擎沒裝」分開：那一句使用者補得了，這一句只能看 log。"""
    monkeypatch.setattr(gui, "load_ac", lambda: _FailingBackend())
    monkeypatch.setattr(gui, "_configure_ocr", lambda: None)
    with pytest.raises(GuiError) as excinfo:
        gui.read_text()
    _generic_only(excinfo, "Text recognition failed.")


def test_no_match_on_screen_is_its_own_answer(monkeypatch):
    """比對成功但一處都沒有：回「找不到」，而不是點在 `hits[0]`（空清單會 IndexError，
    折成泛用的失敗句之後使用者分不出是找不到還是壞了）。"""
    monkeypatch.setattr(gui, "load_ac", lambda: _FailingBackend(error=None))
    with pytest.raises(GuiError) as excinfo:
        gui.locate_image("tpl.png")
    assert str(excinfo.value) == "This image was not found on the screen."


@pytest.mark.parametrize("parts", [[], ["500"], ["500", "300", "7"]])
def test_a_point_needs_exactly_two_coordinates(parts):
    """多給一個值時照舊取前兩個，會把使用者打錯的指令默默執行在另一個位置。"""
    with pytest.raises(GuiError) as excinfo:
        gui.parse_xy(parts)
    assert "Two coordinate values" in str(excinfo.value)
    assert gui.parse_xy(["500", "300"]) == (500, 300)


# --------------------------------------------------------------------------
# 按住 / 放開
# --------------------------------------------------------------------------
class _FakeAC:
    """只記錄事件的假後端；不碰真實鍵盤滑鼠。"""

    # 名字刻意跟真表一樣用 Win32 原始名稱（`control` 而不是 `ctrl`），
    # 這樣別名那層有沒有生效測得出來。
    keyboard_keys_table = {"shift": 16, "control": 17, "w": 87, "f13": 124}

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def press_keyboard_key(self, key):
        self.events.append(("down", key))

    def release_keyboard_key(self, key):
        self.events.append(("up", key))

    def press_mouse(self, button, x=None, y=None):
        self.events.append(("mdown", button))

    def release_mouse(self, button, x=None, y=None):
        self.events.append(("mup", button))


@pytest.fixture(name="fake_ac")
def _fake_ac_fixture(monkeypatch):
    fake = _FakeAC()
    monkeypatch.setattr(gui, "_AC", fake)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    gui._HELD_INPUTS.clear()
    try:
        yield fake
    finally:
        gui._HELD_INPUTS.clear()


def test_key_down_registers_and_key_up_clears(fake_ac):
    gui.key_down("shift")
    assert [row[1] for row in gui.held_inputs()] == ["shift"]
    gui.key_up("shift")
    assert gui.held_inputs() == []
    assert fake_ac.events == [("down", "shift"), ("up", "shift")]


def test_key_up_works_even_when_not_registered(fake_ac):
    # bot 重啟後 `_HELD_INPUTS` 是空的，但鍵可能還按著——這時仍然要送得出放開。
    gui.key_up("ctrl")
    assert fake_ac.events == [("up", "control")]   # 別名已解析


def test_parse_key_name_rejects_unknown_and_malformed(fake_ac):
    assert gui.parse_key_name("SHIFT") == "shift"
    for bad in ("", "ctrl+s", "不存在的鍵"):
        with pytest.raises(GuiError):
            gui.parse_key_name(bad)


def test_release_added_since_leaves_pre_existing_holds(fake_ac):
    gui.key_down("ctrl")            # 使用者跑巨集之前就自己按著的
    snapshot = gui.held_snapshot()
    gui.key_down("shift")           # 巨集自己按的
    gui.mouse_button_down("mouse_left")
    released = gui.release_added_since(snapshot)
    assert sorted(released) == ["mouse_left", "shift"]
    assert [row[1] for row in gui.held_inputs()] == ["control"]


def test_release_input_if_stale_only_matches_same_press(fake_ac):
    gui.key_down("w")
    stale = gui.input_pressed_at("key", "w")
    gui.key_up("w")
    gui.key_down("w")               # 放開後又按了一次：舊計時器不該把它放掉
    assert gui.release_input_if_stale("key", "w", stale) is False
    assert [row[1] for row in gui.held_inputs()] == ["w"]
    assert gui.release_input_if_stale(
        "key", "w", gui.input_pressed_at("key", "w")) is True


def test_macro_verbs_cover_hold_and_release():
    for step in ("keydown shift", "keyup shift", "release_keys"):
        gui.validate_macro_step(step)
    with pytest.raises(GuiError):
        gui.validate_macro_step("keydown")          # 缺參數
    with pytest.raises(GuiError):
        gui.validate_macro_step("release_keys now")  # 不吃參數


def _fail_hotkey_at_execution(monkeypatch) -> None:
    """讓 `hotkey` 這一步**執行時**才失敗（驗證照樣通過）。

    2026-09-21 起 `run_macro_program` 在第一個動作之前就把整個程式驗完，所以
    「驗不過的步驟」再也不會在 `keydown` 之後才失敗——這幾支原本拿
    `hotkey ctrl+s`（假鍵名表裡沒有 `s`）當中途失敗，改完之後 `keydown` 根本沒
    發生，`held_inputs() == []` 就變成一句恆真的斷言。中途失敗現在只剩執行端的
    失敗，這裡就模擬那一種。
    """
    def _boom(tokens):
        raise GuiError("送出按鍵組合失敗")

    monkeypatch.setattr(gui, "press_hotkey", _boom)


def test_run_macro_releases_its_own_holds_when_a_step_fails(fake_ac, monkeypatch):
    # `keydown` 之後才失敗的話，那個鍵會一直按著，等於整台電腦被鎖住，而下指令
    # 的人不在現場。
    _fail_hotkey_at_execution(monkeypatch)
    with pytest.raises(GuiError):
        gui.run_macro(["keydown shift", "hotkey ctrl+w"])
    assert ("down", "shift") in fake_ac.events, "前提：失敗之前 `keydown` 真的發生了"
    assert gui.held_inputs() == []


# --------------------------------------------------------------------------
# 檔案進出主機
# --------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 主機路徑的引號正規化
#
# 這條路是限擁有者的「用對話操作整台電腦」：`/host put`、`/host cd`、`/host get`
# 都靠 `resolve_host_path` 決定要寫／要讀哪一個檔案。所以正規化猜錯的代價不是
# 「指令失敗」，是**安靜地操作到另一個地方**。
#
# 2026-09-06 從寬鬆的 `.strip('"').strip("'")` 換成「只脫成對的一層」，與
# `dorossi_backend._dorossi_unquote_dir` 同一條判準（兩份實作，模組邊界不同）。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ('"D:/Work/Foo"', "D:/Work/Foo"),      # 檔案總管「複製路徑」的原樣輸出
    ("'D:/Work/Foo'", "D:/Work/Foo"),
    ('  "D:/Work/Foo"  ', "D:/Work/Foo"),  # 引號外面還有空白
    ('"  D:/Work/Foo  "', "D:/Work/Foo"),  # 引號裡面還有空白
    ("D:/Work/Foo", "D:/Work/Foo"),        # 沒引號時原樣通過
    ("", ""),
    ("   ", ""),
])
def test_unquote_path_strips_one_matched_pair(raw, expected):
    assert gui.unquote_path(raw) == expected


def test_unquote_path_tolerates_none():
    assert gui.unquote_path(None) == ""


@pytest.mark.parametrize("raw", ['"D:/x', 'D:/x"', "'D:/x\"", '"'])
def test_unquote_path_leaves_an_unmatched_quote_alone(raw):
    """引號沒配對就原樣留著，讓它自然解析失敗。

    舊寫法會把落單的引號刮掉、湊出一個「看起來像」的路徑——在這條路上猜錯等於
    寫到別的地方去。寧可讓使用者看到失敗。
    """
    assert gui.unquote_path(raw) == raw.strip()


def test_unquote_path_only_removes_one_layer():
    """`'` 在 Windows 檔名裡是**合法的**，所以真的叫 `'foo'` 的目錄存在得了。

    舊的 `.strip("'")` 會把它悄悄變成 `foo`——不是拒絕，是指到另一個地方。
    """
    assert gui.unquote_path("''foo''") == "'foo'"
    assert gui.unquote_path('""foo""') == '"foo"'


def test_shell_cwd_accepts_a_quoted_directory(tmp_path):
    """`/host cd "D:\\有空格的資料夾"` 要能用。

    這一支是變異測試逼出來的：把 `set_shell_cwd` 的正規化拿掉時，其他測試全部
    照樣綠——沒有任何一支拿引號路徑餵過它。而這正是**最會被貼引號的入口**，
    因為 `cd` 的參數幾乎都是從檔案總管複製過來的。
    """
    target = tmp_path / "with space"
    target.mkdir()
    saved = gui.shell_cwd()
    try:
        got = gui.set_shell_cwd(f'"{target}"')
        assert got.resolve() == target.resolve()
        got = gui.set_shell_cwd(f"'{target}'")
        assert got.resolve() == target.resolve()
    finally:
        gui.set_shell_cwd(str(saved))


def test_host_path_normalisation_has_exactly_one_implementation():
    """`_gui_control` 裡不得再出現寬鬆的 `.strip('\"')` 路徑剝除。

    修正前有三份（`resolve_host_path`、`write_host_file`、`set_shell_cwd`），
    三份一模一樣——複製貼上的正規化就是「改一份、漏兩份」的形狀。
    """
    import ast
    from pathlib import Path
    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "strip"):
            continue
        if (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in ('"', "'")):
            offenders.append(node.lineno)
    assert not offenders, (
        f"_gui_control.py:{offenders} 又出現了寬鬆的剝引號。主機路徑的正規化"
        "只能有一份，在 `unquote_path`——它只脫成對的一層，因為 `'` 是合法的"
        "Windows 檔名字元，刮掉它等於指到另一個路徑。")


def test_resolve_host_path_expands_and_absolutises():
    assert gui.resolve_host_path("  'x/y.txt'  ") == gui.PROJECT_ROOT / "x" / "y.txt"
    assert gui.resolve_host_path(str(gui.PROJECT_ROOT / "a.txt")).is_absolute()
    with pytest.raises(GuiError):
        gui.resolve_host_path("   ")


def test_safe_basename_strips_traversal():
    assert gui.safe_basename("a/b/../c.txt") == "c.txt"
    assert gui.safe_basename("C:\\tmp\\d.bin") == "d.bin"
    for bad in ("", "..", "a/.."):
        with pytest.raises(GuiError):
            gui.safe_basename(bad)


# ---------------------------------------------------------------------------
# Windows 保留名稱與保留字元
# ---------------------------------------------------------------------------
# 語料刻意**不是**照著某一次實測的結果列的（那會變成「照例子寫守門」——本 repo 的
# `feedback_guards_written_from_examples`）。判準是機制：剝掉結尾的點與空白之後，
# 問標準函式庫維護的那份資料。下面兩組要蓋住那條規則的正反兩面。
_RESERVED_FILENAMES = (
    # 四個無數字的裝置名
    "NUL", "CON", "PRN", "AUX",
    # 編號的兩族（端點 ＋ 中間各取樣）
    "COM1", "COM5", "COM9", "LPT1", "LPT5", "LPT9",
    # Windows 11 才加的上標變體
    "COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b3",
    # 主控台代號
    "CONIN$", "CONOUT$",
    # 大小寫不分
    "nul", "Nul", "nUl", "con", "com1", "conin$",
    # 有副檔名照樣中
    "NUL.md", "NUL.txt", "CON.json", "NUL.tar.gz", "com1.log", "NUL.tmp",
    # 結尾的空白與點被剝掉之後才比對
    "NUL ", "NUL  ", "NUL.", "NUL. ", "NUL .", "nul .txt",
    # 保留字元與控制字元。⚠️ **這一族是判準選擇的分水嶺。** 判準若寫成
    # 「取第一個點之前的字段」（一個很自然的寫法，也繞得開結尾點那條規則），
    # 下面每一個**點後面**帶保留字元的都會漏掉——實測差 8 筆。
    "report:secret", "report.txt:secret", "a.txt:evil", "x.png:$DATA",
    "a\tb", "a\x00b", "a.txt\tb", "a.txt\x00b",
    "a.b<c", "a.b|c", 'a.b"c', "a.b?c", "a.b*c",
)

# 近似但**合法**的名字。這一組不是湊數：`rstrip(". ")` 那一步**只能**由這一組殺
# 掉——它防的是過度攔截，不是漏抓。只列必擋的話那個變異會活下來。
_NEAR_MISS_FILENAMES = (
    # 裝置名是嚴格前綴 / 後綴 / 子字串
    "NULL", "CONSOLE", "AUXILIARY", "PRNT", "PRINTER", "CONTRACT", "AUXIN",
    "NULx", "myNUL", "NUL1", "1NUL", "NUL_1", "NUL-1", "PRN_backup",
    "a NUL b", "NUL b", "b NUL", "COM1A", "ACOM1",
    # 編號超出 1..9（含上標）
    "COM0", "COM10", "LPT0", "LPT10", "COM", "LPT", "COM\u2074", "COM1\u00b9",
    # 裝置名在點的**後面**，所以不是被比對的那一段
    "a.NUL",
    # 尋常檔名
    "upload.bin", ".gitignore", "report.pdf", "\u5831\u544a.txt",
    "debug_char1_0003.png", "session_12345.json", "a.tar.gz",
    # ⚠️ 這三筆殺的是 `rstrip(". ")`：少了它，任何「副檔名前面有空白」的檔名都會
    # 被 `ntpath.isreserved` 的「結尾空白」那一支判成保留名稱而誤擋。
    "my report .txt", "a NUL .txt", "\u5831\u544a .txt",
)


def _reserved_verdict(name: str) -> bool:
    """`safe_basename` 有沒有把這個名字當成保留名稱擋下來。"""
    try:
        gui.safe_basename(name)
    except GuiError:
        return True
    return False


def test_safe_basename_rejects_windows_reserved_names():
    """保留名稱寫下去不會產生使用者要的那個檔案，所以要當場拒絕。

    ⚠️ **這道守衛刻意比「這台機器上真的會出事的名字」寬，不要照著實測收窄。**
    2026-09-11 在本機（Windows 11 build 26200）以 `CreateFileW` ＋ `GetFileType`
    量過：只有 `NUL` 家族真的被路徑剖析器改寫成裝置，`CON` / `PRN` / `AUX` /
    `COM1` / `CONIN$` 全部產生**普通檔案**。舊版 Windows 不是這樣，而標準函式庫
    自己的註解就寫著「規則複雜且隨版本不同，保守起見一律回 True」。在這台機器上
    試出 `CON` 沒事就把它從語料拿掉的話，`write_host_file` 的行為會變成跟著主機的
    Windows 版本跑——那是 fresh clone 會踩到而本機永遠看不到的坑。
    """
    allowed = [n for n in _RESERVED_FILENAMES if not _reserved_verdict(n)]
    assert not allowed, f"這些保留名稱沒有被擋下來：{allowed}"
    # 正面對照組：語料要真的還在。一支恆丟 GuiError 的 `safe_basename` 會讓上面那
    # 句全綠，所以下一支（近似名字必須放行）是這一支的另一半，兩支缺一不可。
    assert len(_RESERVED_FILENAMES) >= 40, "語料被砍掉了"


def test_safe_basename_keeps_near_miss_names_and_they_really_land(tmp_path):
    """只擋真正的那些。近似名字放行，而且放行的必須真的寫得進去。

    第二段是機制對帳，不是重複：它問的是「這道守衛允許的東西，資料真的落在磁碟上
    嗎」。只驗回傳值的話，一個漏抓的守衛跟一個正確的守衛長得一模一樣。
    """
    blocked = [n for n in _NEAR_MISS_FILENAMES if _reserved_verdict(n)]
    assert not blocked, (
        f"這些合法檔名被誤擋了：{blocked}。判準是「剝掉結尾的點與空白之後問 "
        "`ntpath.isreserved`」，不是子字串比對。")
    for name in _NEAR_MISS_FILENAMES:
        base = gui.safe_basename(name)
        target = tmp_path / base
        target.write_bytes(b"12345")
        assert target.stat().st_size == 5, f"{name!r} 放行了卻寫不進去"
        target.unlink()


def test_the_reserved_name_rule_still_lets_trailing_dot_names_through():
    """刻意**不**涵蓋的那一半，明寫出來免得下次被當成漏洞補掉。

    `ntpath.isreserved` 對結尾是點的名字也回 True，但 `_reject_reserved_filename`
    先 `rstrip(". ")`，所以 `...` / `....` / `x.` 照樣通過。那是既有裁定：
    `test_bot_helpers.test_the_attachment_name_guard_lets_nothing_escape` 釘著全點
    名字要原樣通過（Windows 去掉結尾的點之後目標就是目的資料夾自己，`os.replace`
    丟 errno 13 **大聲**失敗，不會靜靜寫錯地方）。要改這個決定就去改那一支，不要
    從這裡偷偷放寬。
    """
    for name in ("...", "....", "x.", ".gitignore"):
        assert gui.safe_basename(name) == name, (
            f"{name!r} 現在被保留名稱守衛擋下來了——那會讓 test_bot_helpers 的 "
            "`test_the_attachment_name_guard_lets_nothing_escape` 一起變紅，"
            "而那一支釘的是一個刻意的裁定。")


def test_write_host_file_rejects_a_reserved_name_before_the_exists_check(tmp_path):
    """明寫路徑那條不經過 `safe_basename`，而且順序是承重的。

    `Path(r"...\\NUL").exists()` 是 True，所以擋晚了的話使用者先拿到「目的檔已經
    存在；要覆寫請加上 `--force`」——一句把他導向 `--force`、而 `--force` 之後只會
    拿到「檔案寫入失敗。」（2026-09-11 實測的修正前實際下場：三句回覆裡有兩句是
    錯的答案，而這是一個擁有者用來操作一台他不在現場的機器的指令）。
    """
    with pytest.raises(GuiError) as explicit:
        gui.write_host_file(str(tmp_path / "NUL"), b"payload")
    assert "reserved" in str(explicit.value) or "specially" in str(explicit.value), (
        "擋在 `path.exists()` 後面了——使用者會先看到「目的檔已經存在」。")
    # `--force` 也要是同一句，不然「加上 --force」就變成一條死路。
    with pytest.raises(GuiError) as forced:
        gui.write_host_file(str(tmp_path / "NUL"), b"payload", overwrite=True)
    assert "specially" in str(forced.value)
    # 附件檔名那條（目的地是資料夾）同樣要擋。
    with pytest.raises(GuiError) as attached:
        gui.write_host_file(str(tmp_path), b"payload", default_name="COM1")
    assert "specially" in str(attached.value)
    # 正面對照組：一般檔名照樣寫得進去，不然「全部拒絕」也會讓上面三句全綠。
    path, written = gui.write_host_file(
        str(tmp_path), b"payload", default_name="NULL.bin")
    assert written == 7 and path.read_bytes() == b"payload"
    assert not list(tmp_path.glob("*.tmp")), "被擋下來的那幾次留了暫存檔"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 的裝置名行為")
def test_the_null_device_really_swallows_data(tmp_path):
    """這道守衛的存在理由，用機制釘住而不是用註解寫著。

    `NUL` 是這台機器上唯一真的被路徑剖析器改寫成裝置的名字（2026-09-11 量的），
    而它的失敗形態正是最糟的那種：寫入回報成功、位元組直接消失、目錄裡什麼都沒有。
    哪天這件事不成立了（Windows 連 `NUL` 都不再特判），上面那些語料的理由要重新
    檢討——所以這一支盯的是**前提**，不是本專案的程式碼。
    """
    with open(tmp_path / "NUL", "wb") as handle:
        handle.write(b"12345")
    assert [p.name for p in tmp_path.iterdir()] == [], (
        "`NUL` 產生了一個真的檔案——保留名稱守衛的前提要重驗。")


def test_an_alternate_data_stream_really_hides_the_bytes(tmp_path):
    """`:` 那一族的存在理由，同樣用機制釘住。

    `report.txt:secret` 寫下去會產生一個 NTFS 替代資料流：`os.listdir` 只看得到
    `report.txt`，而它是 **0 位元組**。這是本專案最在意的那種失效——回報成功、
    資料看不見。判準若改成「取第一個點之前的字段」，這一族會整族漏掉（實測 8 筆），
    所以這一支跟 `_RESERVED_FILENAMES` 裡那幾格是一組的。
    """
    (tmp_path / "report.txt:secret").write_bytes(b"hidden-payload")
    listed = sorted(p.name for p in tmp_path.iterdir())
    assert listed == ["report.txt"], f"ADS 的行為變了：{listed}"
    assert (tmp_path / "report.txt").stat().st_size == 0, (
        "ADS 不再是隱藏的了——`:` 那一族的擋法可以重新檢討。")


def test_the_reserved_name_data_still_comes_from_the_standard_library():
    """前提測試：判準的單一來源是 `ntpath.isreserved`，不是本專案的抄本。

    這一支盯的是**上游**。標準函式庫哪天改掉那份資料或那幾條規則，這裡要當場知道，
    而不是等到某個附件靜靜地寫進裝置裡。同時釘住模組層別名還在——它的作用是讓缺少
    `isreserved` 的直譯器在**匯入時**就炸掉，而不是讓 `AttributeError` 被 `cmd_put`
    的 broad except 折成一句「檔案寫入失敗」。
    """
    import ntpath

    assert gui._isreserved_name is ntpath.isreserved, (
        "保留名稱的判準不再是標準函式庫那一份了——改成本地抄本的話，它會隨著 "
        "Windows 新增裝置名而過期，而過期不會有任何症狀。")
    for name in ("NUL", "CON", "COM9", "LPT1", "CONIN$", "COM\u00b9"):
        assert ntpath.isreserved(name), f"上游不再認得 {name!r}"
    for name in ("NULL", "COM10", "COM0", "CONSOLE", "COM"):
        assert not ntpath.isreserved(name), f"上游開始誤判 {name!r}"


def test_read_host_file_round_trips_and_reports_size_limit(tmp_path, monkeypatch):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"hello")
    assert gui.read_host_file(str(target))[1] == b"hello"
    monkeypatch.setattr(gui, "GET_MAX_BYTES", 1)
    with pytest.raises(GuiError) as excinfo:
        gui.read_host_file(str(target))
    assert "too large" in str(excinfo.value)


def test_read_host_file_distinguishes_missing_from_directory(tmp_path):
    with pytest.raises(GuiError) as missing:
        gui.read_host_file(str(tmp_path / "nope"))
    assert "not found" in str(missing.value)
    with pytest.raises(GuiError) as folder:
        gui.read_host_file(str(tmp_path))
    assert "folder" in str(folder.value)


def test_write_host_file_refuses_overwrite_without_flag(tmp_path):
    target = tmp_path / "a.txt"
    gui.write_host_file(str(target), b"one")
    assert target.read_bytes() == b"one"
    with pytest.raises(GuiError) as excinfo:
        gui.write_host_file(str(target), b"two")
    assert "already exists" in str(excinfo.value)
    assert gui.write_host_file(str(target), b"two", overwrite=True)[1] == 3
    assert target.read_bytes() == b"two"


def test_write_host_file_uses_attachment_name_for_a_folder(tmp_path):
    path, written = gui.write_host_file(
        str(tmp_path), b"xyz", default_name="../evil/report.csv")
    assert path == tmp_path / "report.csv"   # 路徑穿越被剝掉
    assert written == 3


def test_write_host_file_does_not_create_missing_parents(tmp_path):
    # 打錯一個字就多出一串空目錄，而下指令的人不在電腦前面看不到。
    with pytest.raises(GuiError) as excinfo:
        gui.write_host_file(str(tmp_path / "no" / "such" / "a.txt"), b"x")
    assert "destination folder does not exist" in str(excinfo.value)
    assert not (tmp_path / "no").exists()


def test_write_host_file_leaves_no_temp_behind(tmp_path):
    gui.write_host_file(str(tmp_path / "ok.txt"), b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["ok.txt"]


# --------------------------------------------------------------------------
# 指令執行的持久工作目錄
# --------------------------------------------------------------------------
def test_shell_cwd_is_sticky_and_resets(tmp_path):
    try:
        assert gui.shell_cwd() == gui.PROJECT_ROOT
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert gui.set_shell_cwd(str(nested)) == nested
        assert gui.shell_cwd() == nested
        # 相對路徑以**目前**目錄為基準，連續 cd 才符合直覺
        assert gui.set_shell_cwd("..") == nested.parent
        assert gui.set_shell_cwd(None) == gui.PROJECT_ROOT
    finally:
        gui.set_shell_cwd(None)


def test_shell_cwd_rejects_missing_folder(tmp_path):
    try:
        with pytest.raises(GuiError):
            gui.set_shell_cwd(str(tmp_path / "nope"))
        assert gui.shell_cwd() == gui.PROJECT_ROOT      # 失敗不該動到狀態
    finally:
        gui.set_shell_cwd(None)


# --------------------------------------------------------------------------
# 背景作業
# --------------------------------------------------------------------------
def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_job_runs_in_background_and_captures_output():
    # `run_shell` 會擋到跑完，長工作因此不是「慢」而是根本跑不完；`job_*` 是
    # 那條路。這個測試真的起一個子行程（副作用侷限在子行程裡）。
    job_id = gui.job_start("1..3 | ForEach-Object { $_ }"
                           if sys.platform.startswith("win") else "seq 3")
    try:
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
        info = gui.job_log(job_id)
        assert info["rc"] == 0
        assert info["text"].split() == ["1", "2", "3"]
        assert any(row["id"] == job_id for row in gui.job_list())
    finally:
        gui.job_clear()


@pytest.mark.parametrize("running, finished, kept", [
    (3, gui.JOB_MAX_KEPT + 2, gui.JOB_MAX_KEPT),      # 超過：只丟最舊的已結束作業
    (gui.JOB_MAX_KEPT + 2, 0, gui.JOB_MAX_KEPT + 2),  # 超過但全都還在跑：一筆都不丟
    (0, gui.JOB_MAX_KEPT, gui.JOB_MAX_KEPT),          # 剛好在上限：不丟
], ids=["over-cap", "all-running", "at-cap"])
def test_job_pruning_drops_only_the_oldest_finished_jobs(monkeypatch, running, finished, kept):
    """作業紀錄的保留上限在整個套件裡從來沒有超過過（2026-09-22 分支覆蓋率）。

    還在跑的作業一筆都不能丟——丟了就再也 `/host job stop` 不到它，而它的行程還活著。"""
    jobs = {i: {"id": i, "finished": None} for i in range(running)}
    for offset in range(finished):
        jid = running + offset
        jobs[jid] = {"id": jid, "finished": 1000.0 + offset}
    monkeypatch.setattr(gui, "_JOBS", jobs)
    gui._job_prune()
    assert len(gui._JOBS) == kept, sorted(gui._JOBS)
    assert set(range(running)) <= set(gui._JOBS), "還在跑的作業被丟掉了"
    dropped = running + finished - kept
    oldest = set(range(running, running + dropped))
    assert not (oldest & set(gui._JOBS)), f"該丟的是最舊的 {dropped} 筆已結束作業"


def test_job_stop_reports_whether_it_did_anything():
    job_id = gui.job_start("Start-Sleep -Seconds 60"
                           if sys.platform.startswith("win") else "sleep 60")
    try:
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is True, timeout=10)
        assert gui.job_stop(job_id) is True
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
        assert gui.job_log(job_id)["stopped"] is True
        assert gui.job_stop(job_id) is False        # 已經結束了
    finally:
        gui.job_clear()


def test_a_finished_job_releases_its_pipes():
    """作業跑完就該把管道關掉，不要留給參照計數的時機。

    這不是「修一個洩漏」——實測（Windows handle 計數）`job_clear()` 之後本來就會歸零。
    是兩件別的事：(a) 還沒被清掉的已完成作業原本一筆握 2～3 個 handle，關掉之後只剩
    行程本身那一個（實測 20 筆從 +40／+60 降到 +20），而 `_JOBS` 上限是 20 筆、bot 會
    連續跑好幾天；(b) 靠參照計數就是在賭沒有參照環（例外的 traceback 抓住 frame 就
    夠了），明確關掉便宜太多。順帶讓 `-W always::ResourceWarning` 掃全套測試變乾淨，
    那條掃描才用得下去。
    """
    job_id = gui.job_start("1..2 | ForEach-Object { $_ }"
                           if sys.platform.startswith("win") else "seq 2")
    try:
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
        proc = gui._job_get(job_id)["proc"]
        assert proc.stdout.closed, "作業結束了，stdout 還開著"
    finally:
        gui.job_clear()


def test_a_finished_interactive_job_releases_its_stdin_too():
    """互動式作業是唯一會拿到 stdin 管道的路徑，也是最該關的那一個：
    只關 stdout 的話，寫入端會一直開著，而 `_JOBS` 可以留 20 筆。"""
    job_id = gui.job_start("1..2 | ForEach-Object { $_ }"
                           if sys.platform.startswith("win") else "seq 2",
                           interactive=True)
    try:
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
        proc = gui._job_get(job_id)["proc"]
        assert proc.stdin is not None, "互動式作業應該有 stdin 管道"
        assert proc.stdin.closed, "作業結束了，stdin 還開著"
        assert proc.stdout.closed, "作業結束了，stdout 還開著"
    finally:
        gui.job_clear()


def test_clearing_a_job_that_never_started_a_reader_still_closes_it():
    """保險那一層：reader 執行緒沒起來的話，沒有人關過管道。`job_clear` 要補上。"""
    job_id = gui.job_start("1..2 | ForEach-Object { $_ }"
                           if sys.platform.startswith("win") else "seq 2")
    assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
    job = gui._job_get(job_id)
    # 假裝 reader 沒關過：換一支新的、開著的串流上去。
    handle = open(os.devnull, "r", encoding="utf-8")
    job["proc"].stdout = handle
    try:
        gui.job_clear()
        assert handle.closed, "`job_clear` 沒有關掉殘留的管道"
    finally:
        if not handle.closed:
            handle.close()


def test_closing_streams_never_raises():
    """`_close_job_streams` 在收尾路徑上跑，任何情況都不得拋——關第二次、
    沒有 proc、proc 沒有那些屬性，全部要安靜通過。"""
    gui._close_job_streams({})
    gui._close_job_streams({"proc": None})
    gui._close_job_streams({"proc": object()})
    closed = open(os.devnull, "r", encoding="utf-8")
    closed.close()

    class _Boom:
        stdout = property(lambda self: (_ for _ in ()).throw(OSError("nope")))
        stderr = None
        stdin = None

    gui._close_job_streams({"proc": _Boom()})


def test_job_lookup_rejects_unknown_id():
    for call in (lambda: gui.job_log(999_999), lambda: gui.job_stop(999_999)):
        with pytest.raises(GuiError):
            call()


def test_job_start_requires_a_command():
    with pytest.raises(GuiError):
        gui.job_start("   ")


# --------------------------------------------------------------------------
# 動作錄製 → 巨集步驟
# --------------------------------------------------------------------------
def _event(kind, t, **rest):
    return {"kind": kind, "t": t, **rest}


# 轉換用的按鍵→字元表現在由函式庫提供；測試給一份固定的，才不依賴主機配置。
_US = {0x48: ("h", "H"), 0x49: ("i", "I"), 0x57: ("w", "W"),
       0xBF: ("/", "?")}


def test_record_to_steps_covers_click_drag_scroll_and_typing(fake_ac):
    # 用假的鍵名表，讓轉換不依賴主機上有沒有真的後端。
    events = [
        _event("mdown", 0.0, button="left", x=100, y=200),
        _event("mup", 0.05, button="left", x=101, y=200),
        _event("mdown", 3.0, button="left", x=10, y=10),
        _event("mup", 3.4, button="left", x=300, y=120),
        _event("wheel", 4.0, delta=-3),      # 函式庫回的已經是格數
        _event("kdown", 6.0, vk=0x48),
        _event("kdown", 6.05, vk=0x49),
    ]
    assert gui.record_to_steps(events, char_table=_US) == [
        "click 101 200",
        "wait 3",
        "drag 10 10 300 120",
        "wait 0.6",
        "scroll -3",
        "wait 2",
        "type hi",
    ]


def test_record_to_steps_inserts_waits_only_for_real_pauses(fake_ac):
    events = [
        _event("mdown", 0.0, button="left", x=1, y=1),
        _event("mup", 0.02, button="left", x=1, y=1),
        _event("mdown", 0.1, button="left", x=50, y=50),   # 間隔太短，不插 wait
        _event("mup", 0.12, button="left", x=50, y=50),
    ]
    assert "wait" not in " ".join(gui.record_to_steps(events))


def test_record_to_steps_merges_a_double_click(fake_ac):
    events = [
        _event("mdown", 0.0, button="left", x=40, y=40),
        _event("mup", 0.02, button="left", x=40, y=40),
        _event("mdown", 0.15, button="left", x=41, y=40),
        _event("mup", 0.17, button="left", x=41, y=40),
    ]
    assert gui.record_to_steps(events) == ["dclick 41 40"]


def test_record_to_steps_keeps_modifier_combos_out_of_typed_text(fake_ac):
    events = [
        _event("kdown", 0.0, vk=17),        # ctrl 按下
        _event("kdown", 0.02, vk=0x57),     # w
        _event("kup", 0.05, vk=0x57),
        _event("kup", 0.08, vk=17),
        _event("kdown", 1.0, vk=16),        # shift 按下
        _event("kdown", 1.02, vk=0x57),     # 大寫 W 併進 type
        _event("kup", 1.1, vk=16),
    ]
    assert gui.record_to_steps(events, char_table=_US) == [
        "hotkey ctrl+w", "wait 1", "type W"]


def test_record_to_steps_drops_steps_that_would_not_validate(fake_ac):
    # 表裡查不到的鍵沒辦法變成合法步驟；整個巨集不該因此被 `load_macro` 拒收。
    events = [_event("kdown", 0.0, vk=0xFF), _event("kdown", 1.0, vk=0x57)]
    steps = gui.record_to_steps(events, char_table=_US)
    assert steps == ["type w"]
    for step in steps:
        gui.validate_macro_step(step)


def test_record_to_steps_uses_the_recorded_keyboard_layout(fake_ac, monkeypatch):
    # 標點的虛擬鍵碼在不同配置上印出不同的字。假裝一個「`/` 那個鍵印出 `-`」的
    # 配置，轉出來的字必須跟著變，不能寫死 US 那份。
    monkeypatch.setattr(gui, "_record_char_table", lambda: {0xBF: ("-", "_")})
    assert gui.record_to_steps([_event("kdown", 0.0, vk=0xBF)]) == ["type -"]


def test_record_to_steps_asks_the_library_for_the_recorded_layout(monkeypatch):
    # 配置要在**開始錄的時候**問（停止時前景視窗已經換了），所以轉換時用的是
    # 那時候記下來的那一個。
    asked = []

    class _Backend:
        @staticmethod
        def char_table(layout):
            asked.append(layout)
            return {0x41: ("a", "A")}

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    monkeypatch.setattr(gui, "_RECORD_LAYOUT", 0xABCD)
    assert gui._record_char_table() == {0x41: ("a", "A")}
    assert asked == [0xABCD]


def test_record_char_table_survives_a_backend_failure(monkeypatch):
    class _Backend:
        @staticmethod
        def char_table(_layout):
            raise RuntimeError("no layout")

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui._record_char_table() == {}


def test_from_timeline_rebuilds_absolute_times(monkeypatch):
    # 函式庫回的是間隔；轉步驟要判斷停頓與連點，用累加出來的絕對時間比較好寫。
    events = [
        {"op": "key_down", "vk": 65, "delta_ms": 0},
        {"op": "key_up", "vk": 65, "delta_ms": 250},
        {"op": "scroll", "delta": -3, "delta_ms": 500},
        {"op": "layout_noise", "delta_ms": 10},      # 不認得的動詞直接略過
    ]
    out = gui._from_timeline(events)
    assert [(e["kind"], round(e["t"], 3)) for e in out] == [
        ("kdown", 0.0), ("kup", 0.25), ("wheel", 0.75)]


# --------------------------------------------------------------------------
# 鎖定的螢幕
# --------------------------------------------------------------------------
def test_input_desktop_probe_returns_a_bool():
    assert isinstance(gui.input_desktop_available(), bool)


def test_input_primitives_refuse_when_the_machine_is_locked(fake_ac, monkeypatch):
    # 鎖定時送出去的輸入會**安靜地**消失（API 回成功、什麼都沒發生），而下指令
    # 的人不在電腦前面。寧可明確失敗。
    monkeypatch.setattr(gui, "input_desktop_available", lambda: False)
    for call in (lambda: gui.mouse_click("mouse_left", 1, 1),
                 lambda: gui.mouse_move(1, 1),
                 lambda: gui.type_text("x"),
                 lambda: gui.press_hotkey(["shift"]),
                 lambda: gui.key_down("shift")):
        with pytest.raises(GuiError) as excinfo:
            call()
        assert "locked" in str(excinfo.value)


def test_releasing_still_works_when_locked(fake_ac, monkeypatch):
    # 放開是**復原路徑**，不能被閘門擋住——否則卡住的鍵永遠解不掉。
    gui.key_down("shift")
    monkeypatch.setattr(gui, "input_desktop_available", lambda: False)
    assert gui.key_up("shift") == "shift"
    assert gui.held_inputs() == []


def test_input_desktop_result_is_cached_briefly(monkeypatch):
    # 巨集每一步都會問，所以要快取；但不能久到「解鎖了還說鎖著」。快取在函式庫
    # 那邊，這裡驗它真的有快取、也真的會過期。
    if not sys.platform.startswith("win"):
        pytest.skip("非 Windows 一律回 True，沒有快取行為可驗")
    reach = pytest.importorskip(
        "je_auto_control.utils.input_reach.input_reach", reason="需要函式庫")
    original = reach._desktop_cache
    try:
        reach._desktop_cache = (time.monotonic(), False)
        assert gui.input_desktop_available() is False        # 用快取，不打 API
        reach._desktop_cache = (
            time.monotonic() - reach.DESKTOP_CACHE_SEC - 1, False)
        assert gui.input_desktop_available() is True         # 過期就重新探測
    finally:
        reach._desktop_cache = original


def test_input_probes_report_available_when_they_cannot_tell(monkeypatch):
    # 這兩個是為了給出更好的錯誤訊息，不該反過來擋住操作——測不出來就當可用。
    class _Broken:
        @staticmethod
        def input_desktop_available():
            raise RuntimeError("probe blew up")

        @staticmethod
        def input_reaches_system():
            raise RuntimeError("probe blew up")

    monkeypatch.setattr(gui, "load_ac", lambda: _Broken)
    assert gui.input_desktop_available() is True
    assert gui.input_reaches_system() is True


def test_input_reaches_system_reports_a_filtered_desktop(monkeypatch):
    # 前景是有防作弊的遊戲時輸入會被整個丟掉，而鎖定偵測看不出來。
    class _Filtered:
        @staticmethod
        def input_reaches_system():
            return False

    monkeypatch.setattr(gui, "load_ac", lambda: _Filtered)
    assert gui.input_reaches_system() is False


# --------------------------------------------------------------------------
# 視窗幾何
# --------------------------------------------------------------------------
def test_window_rect_and_move_report_no_match_clearly(monkeypatch):
    monkeypatch.setattr(gui, "_window_api", lambda: object())
    monkeypatch.setattr(gui, "match_windows", lambda needle: [])
    for call in (lambda: gui.window_rect("nope"),
                 lambda: gui.window_move("nope", 0, 0)):
        with pytest.raises(GuiError) as excinfo:
            call()
        assert "No matching window" in str(excinfo.value)


def _no_backend():
    raise AssertionError("不該走到桌面自動化後端")


def test_window_close_and_snap_report_no_match_clearly(monkeypatch):
    """沒有命中的視窗時要說「找不到」，而且**不能碰後端**。少了這道，下一行的
    `matched[0]` 丟出來的是原始的 `IndexError`，不是一句使用者看得懂的話
    （2026-09-23 分支覆蓋率：兩個函式的這一邊都從來沒跑過）。"""
    monkeypatch.setattr(gui, "_window_api", lambda: object())
    monkeypatch.setattr(gui, "load_ac", _no_backend)
    monkeypatch.setattr(gui, "match_windows", lambda needle: [])
    for call in (lambda: gui.window_close("nope"),
                 lambda: gui.snap_window("nope", "left")):
        with pytest.raises(GuiError) as excinfo:
            call()
        assert "No matching window" in str(excinfo.value)


def test_a_snap_the_library_declined_is_not_reported_as_done(monkeypatch):
    """後端回 False（沒有排成）時要報失敗。少了這道，呼叫端會回「已靠左」而視窗根本沒動。"""
    class _Declines:
        @staticmethod
        def snap_window(_title, _key):
            return False

    monkeypatch.setattr(gui, "load_ac", lambda: _Declines())
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(7, "Editor")])
    with pytest.raises(GuiError) as excinfo:
        gui.snap_window("Editor", "left")
    assert "Failed to snap the window" in str(excinfo.value)


@pytest.mark.parametrize("name", ["", "   ", None])
def test_process_running_refuses_an_empty_name(monkeypatch, name):
    """後端的比對是「不分大小寫的包含比對」，而空字串包含在每一個行程名稱裡——少了這道，
    空名稱會被回報成「有在跑」，監看與巨集的條件當場成立。"""
    monkeypatch.setattr(gui, "load_ac", _no_backend)
    with pytest.raises(GuiError):
        gui.process_running(name)


class _NoWrites:
    def write(self, _text):
        raise AssertionError("不該寫進去")

    def flush(self):
        raise AssertionError("不該寫進去")


@pytest.mark.parametrize("job, wording", [
    ({"interactive": True, "finished": 1.0,
      "proc": types.SimpleNamespace(stdin=_NoWrites())}, "already finished"),
    ({"interactive": True, "finished": None,
      "proc": types.SimpleNamespace(stdin=None)}, "no writable"),
], ids=["finished", "no-stdin"])
def test_job_send_refuses_a_job_that_cannot_take_input(monkeypatch, job, wording):
    monkeypatch.setattr(gui, "_JOBS", {5: job})
    with pytest.raises(GuiError) as excinfo:
        gui.job_send(5, "yes")
    assert wording in str(excinfo.value)


def test_window_rect_returns_position_and_size(monkeypatch):
    class _Api:
        @staticmethod
        def window_rect(needle):
            del needle
            return (10, -20, 810, 380)

    monkeypatch.setattr(gui, "_window_api", lambda: _Api)
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(1, "t"), (2, "u")])
    hwnd, title, rect, count = gui.window_rect("t")
    assert (hwnd, title, count) == (1, "t", 2)
    assert rect == (10, -20, 800, 400)      # 角座標換成「左上角 ＋ 寬高」


def test_window_rect_reports_failure_when_the_package_cannot_read_it(monkeypatch):
    """套件回 None 代表讀不到；不要把它當成 (0,0,0,0) 這種假座標往下傳。"""
    monkeypatch.setattr(gui, "_window_api",
                        lambda: type("A", (), {"window_rect": staticmethod(
                            lambda needle: None)}))
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(1, "t")])
    with pytest.raises(GuiError) as excinfo:
        gui.window_rect("t")
    assert "Failed to read the window position" in str(excinfo.value)


def test_window_close_goes_through_the_package_not_a_local_win32_call(monkeypatch):
    """視窗操作只有一份實作：套件。本檔不再自己開 pywin32。"""
    seen = []
    monkeypatch.setattr(gui, "_window_api",
                        lambda: type("A", (), {"close_window_by_title":
                                               staticmethod(seen.append)}))
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(7, "Editor")])
    hwnd, title, count = gui.window_close("Edit")
    assert (hwnd, title, count) == (7, "Editor", 1)
    assert seen == ["Edit"]


# --------------------------------------------------------------------------
# 互動式背景作業
# --------------------------------------------------------------------------
def test_shell_argv_keeps_prompts_alive_only_when_interactive():
    if not sys.platform.startswith("win"):
        pytest.skip("PowerShell 旗標只在 Windows 上有意義")
    assert "-NonInteractive" in gui.shell_argv("x")
    # `-NonInteractive` 會讓 `Read-Host` 直接報錯，餵得進輸入就沒意義了
    assert "-NonInteractive" not in gui.shell_argv("x", interactive=True)


def test_job_send_requires_an_interactive_job():
    job_id = gui.job_start("Start-Sleep -Seconds 5"
                           if sys.platform.startswith("win") else "sleep 5")
    try:
        with pytest.raises(GuiError) as excinfo:
            gui.job_send(job_id, "x")
        assert "--stdin" in str(excinfo.value)
    finally:
        gui.job_stop(job_id)
        gui.job_clear()


def test_interactive_job_answers_a_prompt():
    if not sys.platform.startswith("win"):
        pytest.skip("用 PowerShell 的 Read-Host 當互動式對象")
    job_id = gui.job_start('$n = Read-Host "name"; Write-Output "hello $n"',
                           interactive=True)
    try:
        assert _wait_for(lambda: gui.job_log(job_id)["total"] >= 0, timeout=5)
        time.sleep(1.0)      # 等 PowerShell 起來並停在提示上
        gui.job_send(job_id, "world")
        assert _wait_for(lambda: gui.job_log(job_id)["running"] is False)
        assert "hello world" in gui.job_log(job_id)["text"]
    finally:
        gui.job_stop(job_id)
        gui.job_clear()


def test_ocr_lang_for_empty_target_prefers_the_widest_model(monkeypatch):
    # `read_text` 是「畫面上寫了什麼」，不知道會讀到什麼就用涵蓋最廣的那組。
    monkeypatch.setattr(gui, "_load_ocr", lambda: _FakeOcrLangs(["chi_tra", "eng"]))
    assert gui.ocr_lang_for("") == "chi_tra+eng"


def test_record_stop_without_start_is_a_clean_error():
    assert gui.record_active() is False
    with pytest.raises(GuiError):
        gui.record_stop()


# --------------------------------------------------------------------------
# 巨集控制流
# --------------------------------------------------------------------------
def test_macro_block_map_pairs_openers_with_ends():
    steps = ["repeat 2", "if_text a", "wait 0", "else", "wait 0", "end", "end"]
    assert gui.macro_block_map(steps) == {0: (None, 6), 1: (3, 5)}


def test_macro_block_map_rejects_unbalanced_blocks():
    # 少一個 `end` 是很容易犯的錯，而等到重播跑到一半才發現，前面那些步驟已經
    # 在真實桌面上做過了，收不回來。
    for bad, hint in (
        (["repeat 2", "wait 0"], "no matching"),
        (["end"], "extra"),
        (["else"], "`else` must"),
        (["if_text a", "else", "else", "end"], "only one"),
    ):
        with pytest.raises(GuiError) as excinfo:
            gui.macro_block_map(bad)
        assert hint in str(excinfo.value)


def test_macro_repeat_runs_the_body_n_times():
    ran = []
    gui.run_macro_program(["repeat 3", "wait 0", "end"],
                          on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0"] * 3


def test_macro_repeat_zero_skips_the_body():
    ran = []
    gui.run_macro_program(["repeat 0", "wait 0", "end", "wait 0"],
                          on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0"]        # 只有區塊外面那一步


def test_macro_if_else_picks_one_branch(monkeypatch):
    monkeypatch.setattr(gui, "match_windows", lambda needle: [])
    ran = []
    gui.run_macro_program(
        ["if_window nope", "wait 0.01", "else", "wait 0.02", "end"],
        on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0.02"]
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(1, "t")])
    ran.clear()
    gui.run_macro_program(
        ["if_window yes", "wait 0.01", "else", "wait 0.02", "end"],
        on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0.01"]


def test_macro_stop_ends_early():
    ran = []
    gui.run_macro_program(["wait 0", "stop", "wait 0"],
                          on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0"]


def test_macro_arguments_are_substituted():
    assert gui.substitute_macro_args("type $1 and $2", ["a", "b"]) == "type a and b"
    # 沒給的參數換成空字串，而不是留下 `$3` 讓它被當成字面文字送出去
    assert gui.substitute_macro_args("type $3", ["a"]) == "type "


def test_macro_execution_budget_stops_runaway_loops():
    # 巢狀 `repeat` 只要四行就能要求跑一百萬步；來源行數上限完全擋不住。
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(
            ["repeat 1000", "repeat 1000", "wait 0", "end", "end"])
    assert "number of executed steps exceeded the limit" in str(excinfo.value)


def test_macro_call_depth_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "MACRO_DIR", tmp_path)
    gui.save_macro("selfcall", ["call selfcall"])
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(gui.load_macro("selfcall")["steps"])
    assert "call depth exceeds the limit" in str(excinfo.value)


def test_macro_call_runs_the_other_macro(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "MACRO_DIR", tmp_path)
    gui.save_macro("inner", ["wait 0", "wait 0"])
    ran = []
    gui.run_macro_program(["repeat 2", "call inner", "end"],
                          on_step=lambda i, s, d: ran.append(s))
    assert ran.count("wait 0") == 4


def test_edit_macro_revalidates_the_whole_program(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "MACRO_DIR", tmp_path)
    gui.save_macro("m", ["repeat 2", "wait 0", "end"])
    assert gui.edit_macro("m", 2, "wait 1") == ["repeat 2", "wait 1", "end"]
    # 刪掉 `end` 會破壞區塊平衡——只驗那一行的話，下次重播才會爆
    with pytest.raises(GuiError):
        gui.edit_macro("m", 3, None)
    assert gui.load_macro("m")["steps"] == ["repeat 2", "wait 1", "end"]
    assert gui.edit_macro("m", 1, "wait 2", insert=True)[0] == "wait 2"


# --------------------------------------------------------------------------
# 等待消失 / 等待顏色
# --------------------------------------------------------------------------
def test_parse_color_accepts_hex_and_rgb():
    assert gui.parse_color("#2ECC71") == (46, 204, 113)
    assert gui.parse_color("2ecc71") == (46, 204, 113)
    assert gui.parse_color("30,144,255") == (30, 144, 255)
    for bad in ("", "#12345", "300,0,0", "red"):
        with pytest.raises(GuiError):
            gui.parse_color(bad)


def test_wait_pixel_tolerates_small_differences(monkeypatch):
    # 抗鋸齒與色彩管理會讓「同一個顏色」差個幾階，要求完全相等的話這個功能在
    # 真實畫面上幾乎不會成立。
    monkeypatch.setattr(gui, "pixel_color", lambda x, y: (100, 100, 100))
    assert gui.wait_pixel(0, 0, (105, 95, 100), 0.5) == (100, 100, 100)
    with pytest.raises(GuiError):
        gui.wait_pixel(0, 0, (200, 100, 100), 0.3)


def test_wait_pixel_can_wait_for_a_change(monkeypatch):
    monkeypatch.setattr(gui, "pixel_color", lambda x, y: (10, 20, 30))
    assert gui.wait_pixel(0, 0, (200, 0, 0), 0.5, match=False) == (10, 20, 30)


def test_wait_gone_returns_once_the_target_disappears(monkeypatch):
    seen = {"n": 0}

    def _match(needle):
        seen["n"] += 1
        return [] if seen["n"] > 1 else [(1, "t")]

    monkeypatch.setattr(gui, "match_windows", _match)
    gui.wait_window_gone("t", 5.0, poll=0.01)
    assert seen["n"] >= 2


def test_wait_gone_gives_up_at_its_deadline_when_the_window_stays(monkeypatch):
    """視窗一直不走時，要在時限到時丟錯。

    這一邊在整個套件裡從來沒有成立過（2026-09-22 分支覆蓋率），而拿掉它不會紅，只會讓
    呼叫端（巨集的 `wait_window_gone`、監看）永遠等下去。替身睡眠數到上限就炸，所以回歸時
    是紅燈而不是掛住；時限給 0，第一輪就該放棄，不必去動時鐘。"""
    naps: list = []

    def _nap(poll, should_abort=None):
        naps.append(poll)
        if len(naps) > 20:
            raise AssertionError("時限早就到了還在等")

    monkeypatch.setattr(gui, "match_windows", lambda needle: [(1, needle)])
    monkeypatch.setattr(gui, "_sleep_abortable", _nap)
    with pytest.raises(GuiError, match="Timed out"):
        gui.wait_window_gone("t", 0.0, poll=0.01)
    assert naps == [], "時限是 0 還睡了一輪"


# --------------------------------------------------------------------------
# UI 元素定位
# --------------------------------------------------------------------------
def test_ui_status_returns_generic_reason():
    ok, reason = gui.ui_status()
    assert isinstance(ok, bool)
    assert reason and "\\" not in reason


def test_parse_ui_type_validates_names_and_rejects_unknown():
    # 只驗證與正規化；真正的比對由函式庫做（它同時吃 `button` 與原始代碼）。
    assert gui.parse_ui_type("Button") == "button"
    assert gui.parse_ui_type("") == ""
    with pytest.raises(GuiError):
        gui.parse_ui_type("wingdings")


# --------------------------------------------------------------------------
# 剪貼簿：檔案清單與內容種類
# --------------------------------------------------------------------------
def test_clipboard_kinds_drops_the_raw_format_list(monkeypatch):
    """格式名稱不往外傳：那是 Win32 的內部名稱，有些程式會把自訂格式取成含
    路徑或產品代號的字串。"""
    class _Backend:
        @staticmethod
        def clipboard_formats():
            return {"categories": ["files", "text"],
                    "formats": [{"id": 15, "name": "CF_HDROP"},
                                {"id": 49999, "name": r"C:\secret\App.Format"}],
                    "has_text": True, "has_image": False, "has_files": True}

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    kinds = gui.clipboard_kinds()
    assert kinds == {"categories": ["files", "text"], "has_text": True,
                     "has_image": False, "has_files": True}
    assert "formats" not in kinds


def test_clipboard_file_list_returns_paths_for_the_caller_to_summarise(
        monkeypatch):
    class _Backend:
        @staticmethod
        def get_clipboard_files():
            return [r"D:\a\one.png", r"D:\a\two.txt"]

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui.clipboard_file_list() == [r"D:\a\one.png", r"D:\a\two.txt"]


def test_clipboard_file_list_reports_a_generic_error(monkeypatch):
    class _Backend:
        @staticmethod
        def get_clipboard_files():
            raise RuntimeError(r"OpenClipboard failed for D:\secret")

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    with pytest.raises(GuiError) as caught:
        gui.clipboard_file_list()
    assert "secret" not in str(caught.value)


# --------------------------------------------------------------------------
# 視窗版面
# --------------------------------------------------------------------------
def test_layout_path_rejects_traversal_and_odd_names(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "LAYOUT_DIR", tmp_path)
    assert gui.layout_path("work-1").parent == tmp_path
    for bad in ("", "../etc", "a/b", "x" * 41, "名稱"):
        with pytest.raises(GuiError):
            gui.layout_path(bad)


def test_restore_layout_says_it_is_missing_instead_of_failing_oddly(
        tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "LAYOUT_DIR", tmp_path)
    with pytest.raises(GuiError):
        gui.restore_window_layout("nope")


def test_list_window_layouts_skips_broken_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "LAYOUT_DIR", tmp_path)
    (tmp_path / "good.json").write_text('[{"title": "a"}, {"title": "b"}]',
                                        encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    rows = gui.list_window_layouts()
    assert [(name, count) for name, count, _mtime in rows] == [("good", 2)]


def test_snap_window_rejects_unknown_positions(monkeypatch):
    monkeypatch.setattr(gui, "match_windows", lambda needle: [(1, "Editor")])
    with pytest.raises(GuiError):
        gui.snap_window("Editor", "diagonal")


def test_snap_positions_are_exactly_what_the_backend_accepts():
    """位置名稱是**值**不是符號，門面契約測試看不到它。

    這條實際踩過：`maximize` 看起來理所當然，但函式庫那邊叫 `max`，送過去只會
    丟 ValueError 然後被折成一句泛用的「靠邊排列失敗」——使用者只會覺得功能壞了。
    這裡拿真的函式庫驗每一個值，並且反向確認亂寫的值它真的會拒絕。
    """
    try:
        backend = gui.load_ac()
    except GuiError:
        pytest.skip("桌面自動化後端在此環境不可用")
    for position in gui.SNAP_POSITIONS:
        # 注入 mover / screen_size：驗的是值域，不動任何真實視窗。
        assert backend.snap_window(
            "irrelevant", position,
            mover=lambda *args: True, screen_size=lambda: (1000, 800)) is True
    for alias, target in gui.SNAP_ALIASES.items():
        assert target in gui.SNAP_POSITIONS, alias
    # 釘住 ValueError，不是裸的 Exception。裸的版本會連「簽章改了所以 TypeError」
    # 「函式名打錯所以 AttributeError」都算通過——而上面那半段測試的全部意義就是
    # 「這些參數還餵得進去」。負面斷言鬆掉的話，正面斷言壞掉時它會幫忙掩護。
    with pytest.raises(ValueError):
        backend.snap_window("irrelevant", "maximize",
                            mover=lambda *args: True,
                            screen_size=lambda: (1000, 800))


def test_snap_window_passes_the_matched_full_title_to_the_backend(monkeypatch):
    """套件那邊是用標題找視窗的，所以要把**命中的完整標題**交回去，不是使用者
    打的片段——片段餵回去會找不到（或找到另一個視窗）。"""
    seen: list[tuple[str, str]] = []

    class _Backend:
        @staticmethod
        def snap_window(title, position):
            seen.append((title, position))
            return True

    monkeypatch.setattr(gui, "match_windows",
                        lambda needle: [(7, "Untitled - Editor")])
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui.snap_window("edit", "LEFT") == (7, "Untitled - Editor")
    assert seen == [("Untitled - Editor", "left")]


def test_grid_windows_names_the_one_it_could_not_find(monkeypatch):
    monkeypatch.setattr(gui, "match_windows",
                        lambda needle: [(1, "Editor")] if needle == "ok" else [])
    with pytest.raises(GuiError) as caught:
        gui.grid_windows(["ok", "missing"])
    assert "missing" in str(caught.value)


# --------------------------------------------------------------------------
# 背景視窗輸入
# --------------------------------------------------------------------------
def test_send_key_to_window_goes_through_the_posting_api(monkeypatch):
    """必須走 `post_key_to_window`（投遞給**有焦點的子控制項**）。

    較舊的 `send_key_event_to_window` 投遞給頂層視窗，實測對任何有子控制項的程式
    都沒有作用——而且它照樣「成功」，是最難發現的那種壞法。
    """
    seen: list[tuple[str, str]] = []

    class _Backend:
        @staticmethod
        def post_key_to_window(title, key):
            seen.append((title, key))
            return True

    monkeypatch.setattr(gui, "match_windows",
                        lambda needle: [(9, "Untitled - Editor")])
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui.send_key_to_window("edit", "enter") == (
        9, "Untitled - Editor", "return")
    assert seen == [("Untitled - Editor", "return")]


def test_send_click_to_window_goes_through_the_posting_api(monkeypatch):
    seen: list[tuple] = []

    class _Backend:
        @staticmethod
        def post_click_to_window(title, button, x, y):
            seen.append((title, button, x, y))
            return True

    monkeypatch.setattr(gui, "match_windows", lambda needle: [(9, "Editor")])
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    gui.send_click_to_window("edit", "right", 40, 12)
    assert seen == [("Editor", "mouse_right", 40, 12)]


def test_background_input_reports_a_failed_post_instead_of_success(monkeypatch):
    """投遞不出去就要說失敗。回 False 卻回報成功等於再造一次靜默成功。"""
    class _Backend:
        @staticmethod
        def post_key_to_window(title, key):
            return False

        @staticmethod
        def post_click_to_window(title, button, x, y):
            return False

    monkeypatch.setattr(gui, "match_windows", lambda needle: [(9, "Editor")])
    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    with pytest.raises(GuiError):
        gui.send_key_to_window("edit", "a")
    with pytest.raises(GuiError):
        gui.send_click_to_window("edit", "left", 1, 1)


def test_background_input_rejects_an_unknown_window_before_calling_backend(
        monkeypatch):
    monkeypatch.setattr(gui, "match_windows", lambda needle: [])
    with pytest.raises(GuiError):
        gui.send_key_to_window("nope", "enter")
    with pytest.raises(GuiError):
        gui.send_click_to_window("nope", "left", 1, 1)


# --------------------------------------------------------------------------
# 等待條件：連接埠 / 行程 / 剪貼簿
# --------------------------------------------------------------------------
def test_parse_host_port_defaults_to_localhost():
    assert gui.parse_host_port("8080") == ("127.0.0.1", 8080)
    assert gui.parse_host_port("example.test:443") == ("example.test", 443)
    assert gui.parse_host_port(" :8080 ") == ("127.0.0.1", 8080)


def test_parse_host_port_rejects_junk_and_out_of_range():
    for bad in ("", "abc", "0", "65536", "host:"):
        with pytest.raises(GuiError):
            gui.parse_host_port(bad)


class _Outcome:
    def __init__(self, ok):
        self.succeeded = ok


def test_wait_predicates_fold_the_backend_outcome_to_a_bool(monkeypatch):
    calls: dict[str, dict] = {}

    class _Backend:
        @staticmethod
        def wait_until_port(host, port, **kwargs):
            calls["port"] = {"host": host, "port": port, **kwargs}
            return _Outcome(True)

        @staticmethod
        def wait_until_process(name, **kwargs):
            calls["process"] = {"name": name, **kwargs}
            return _Outcome(False)

        @staticmethod
        def wait_until_clipboard_changes(**kwargs):
            calls["clip"] = kwargs
            return _Outcome(True)

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui.port_open("127.0.0.1", 22) is True
    assert gui.process_running("notepad.exe") is False
    assert gui.clipboard_changed("before", contains="needle") is True
    assert calls["port"]["port"] == 22
    assert calls["process"]["present"] is True
    # 有 `contains` 時要同時給 target 與 contains 旗標，否則變成「等它變成
    # 這個字串」而不是「等它含這個字串」。
    assert calls["clip"]["target"] == "needle"
    assert calls["clip"]["contains"] is True


def test_wait_predicates_treat_backend_failure_as_not_yet(monkeypatch):
    """探測失敗只代表「還沒成立」。監看不該因為一次失敗就整個結束。"""
    class _Backend:
        @staticmethod
        def wait_until_port(*a, **k):
            raise OSError("boom")

        @staticmethod
        def wait_until_process(*a, **k):
            raise OSError("boom")

        @staticmethod
        def wait_until_clipboard_changes(*a, **k):
            raise OSError("boom")

    monkeypatch.setattr(gui, "load_ac", lambda: _Backend)
    assert gui.port_open("127.0.0.1", 22) is False
    assert gui.process_running("notepad.exe") is False
    assert gui.clipboard_changed("x") is False


# --------------------------------------------------------------------------
# 底層表叫不出名字的鍵（`VK_OEM_*` 等）：錄得到、驗得過、送得出去（2026-09-21）
#
# 底層鍵名表（192 筆）沒有 VK 186–192、219–223、226 這些標點鍵，也沒有 0xAC、
# 0xFE，而 0xB7 只有大寫的 `LAUNCH_APP2`（`parse_key_name` 會先轉小寫，所以寫不
# 出來）。沒有 ctrl/alt/win 時標點鍵走鍵盤配置感知的字元表、變成 `type`，平常打字
# 沒事；一按著修飾鍵就落到 `hotkey` 分支 → 反查不到名字 → `# 未知按鍵` → 存檔前被
# 丟掉。錄製回報「N 步」、重播少一步，而且 `hotkey ctrl+=` 這種組合連手寫都寫不
# 出來。修法在本專案這一側：`_EXTRA_KEY_CODES` 補名字，`_library_key` 在送出那一刻
# 換成整數（函式庫收整數鍵碼）。
#
# 這一段的假後端**照真表查字串、查不到就丟**（與函式庫的 `_resolve_keycode` 同一條
# 路），記錄解析後的鍵碼。所以「補充表的名字沒換成整數就交出去」會在這裡當場失敗，
# 不會像其他假後端那樣什麼名字都收。
# --------------------------------------------------------------------------
class _KeyLibrary:
    """忠於函式庫查表行為的假後端：字串照真表查、查不到就丟；整數原樣收。"""

    def __init__(self, table, *, fail_press_at=None):
        self.keyboard_keys_table = table
        self.raw: list[tuple[str, object]] = []      # 函式庫**收到**的原樣參數
        self.sent: list[tuple[str, object]] = []     # 解析成鍵碼之後
        self.fail_press_at = fail_press_at
        self._presses = 0

    def _resolve(self, key):
        if isinstance(key, int):
            return key
        if key not in self.keyboard_keys_table:
            raise LookupError(f"cannot find key {key!r}")
        return self.keyboard_keys_table[key]

    def input_desktop_available(self):
        return True

    def press_keyboard_key(self, key, is_shift=False, skip_record=False):
        self.raw.append(("press", key))
        self.sent.append(("press", self._resolve(key)))

    def release_keyboard_key(self, key, is_shift=False, skip_record=False):
        self.raw.append(("release", key))
        self.sent.append(("release", self._resolve(key)))

    def hotkey(self, keys, is_shift=False):
        self.raw.append(("hotkey", list(keys)))
        codes = []
        for key in keys:
            self._presses += 1
            code = self._resolve(key)
            if self.fail_press_at == self._presses:
                raise OSError("SendInput failed")
            codes.append(code)
        self.sent.append(("hotkey", codes))
        return "", ""

    def post_key_to_window(self, title, key):
        self.raw.append(("post", key))
        self.sent.append(("post", self._resolve(key)))
        return True


@pytest.fixture(name="key_library")
def _key_library_fixture(real_ac_tables, monkeypatch):
    keys, _ = real_ac_tables
    library = _KeyLibrary(keys)
    monkeypatch.setattr(gui, "_AC", library)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    gui._HELD_INPUTS.clear()
    try:
        yield library
    finally:
        gui._HELD_INPUTS.clear()


# 微軟 "Virtual-Key Codes (Winuser.h)"（2026-09-21 對過）。**刻意在這裡另寫一份**，
# 不從 `_EXTRA_KEY_CODES` 推：拿表自己驗自己，打錯一個鍵碼照樣全綠。
_MICROSOFT_VK = {
    "oem_1": 0xBA, "oem_plus": 0xBB, "oem_comma": 0xBC, "oem_minus": 0xBD,
    "oem_period": 0xBE, "oem_2": 0xBF, "oem_3": 0xC0, "oem_4": 0xDB,
    "oem_5": 0xDC, "oem_6": 0xDD, "oem_7": 0xDE, "oem_8": 0xDF,
    "oem_102": 0xE2, "oem_clear": 0xFE, "launch_app2": 0xB7,
    "browser_home": 0xAC,
}

# 會在非美式配置上說謊的名字。`oem_1`～`oem_8`、`oem_102` 在微軟的定義裡是「依鍵盤
# 而異」，同一顆鍵在別的配置上印出來的是別的字。
_LAYOUT_LYING_NAMES = (
    "semicolon", "slash", "backtick", "grave", "tilde", "lbracket", "rbracket",
    "bracketleft", "backslash", "quote", "apostrophe", "equals", "equal",
)

# 能當修飾鍵錄進 `hotkey` 的三顆（shift 單獨按著不算，會併進 `type`）與它們交給
# 函式庫時的鍵碼。
_HARD_MODIFIERS = ((0x11, 17), (0x12, 18), (0x5B, 91))


def test_the_extra_key_table_matches_the_microsoft_definitions():
    assert gui._EXTRA_KEY_CODES == _MICROSOFT_VK


def test_the_library_accepts_an_integer_keycode_and_rejects_an_unknown_name():
    """修法的前提：函式庫收整數鍵碼，而查不到的字串會丟（假後端照這條路做）。

    這支壞掉代表函式庫的查表行為變了——`_library_key` 換整數的做法與上面那個
    假後端的忠實度都要重新確認。
    """
    keyboard = pytest.importorskip(
        "je_auto_control.wrapper.auto_control_keyboard",
        reason="je-auto-control 沒安裝（requirements.txt 有列，正常不該發生）")
    exceptions = pytest.importorskip(
        "je_auto_control.utils.exception.exceptions",
        reason="je-auto-control 沒安裝（requirements.txt 有列，正常不該發生）")
    assert keyboard._resolve_keycode(0xBB) == 0xBB
    with pytest.raises(exceptions.AutoControlCantFindKeyException):
        keyboard._resolve_keycode("definitely_not_a_key")


def test_every_extra_key_name_is_writable_and_shadows_nothing(real_ac_tables):
    """補充表的名字要寫得出來，而且不能改掉底層表或別名表既有的意思。"""
    keys, _ = real_ac_tables
    unwritable = sorted(name for name in gui._EXTRA_KEY_CODES
                        if name != name.lower()
                        or not gui.HOTKEY_TOKEN_RE.match(name))
    assert not unwritable, f"`parse_key_name` 會先轉小寫、只收英數底線：{unwritable}"
    conflicting = sorted(name for name, code in gui._EXTRA_KEY_CODES.items()
                         if name in keys and keys[name] != code)
    assert not conflicting, (
        f"這些名字底層表也有、卻指向別的鍵：{conflicting}。`_library_key` 會送出補充表"
        "的鍵碼，同一個名字在兩張表裡意思不同就是靜默改道。")
    shadowed = sorted(set(gui.KEY_ALIASES) & set(gui._EXTRA_KEY_CODES))
    assert not shadowed, f"別名先查，這些補充表的名字會被別名蓋掉：{shadowed}"


def test_every_override_name_is_writable_and_shadows_nothing():
    """覆寫表的名字也要寫得出來，而且不能被別名蓋掉、不能跟補充表重疊。

    這**不是**別名：別名是「使用者打 A、其實要 B 這個底層名字」，覆寫是「底層這
    個名字本身指錯了」。所以 `test_no_key_alias_shadows_a_real_key_name` 管不到
    也不必管它——但別名表裡若出現同一個名字，`parse_key_name` 先查別名，覆寫就
    永遠輪不到，要在這裡擋。
    """
    assert gui._LIBRARY_NAME_OVERRIDES, "覆寫表空了，底下每一句都會空轉通過"
    unwritable = sorted(name for name in gui._LIBRARY_NAME_OVERRIDES
                        if name != name.lower()
                        or not gui.HOTKEY_TOKEN_RE.match(name))
    assert not unwritable, f"`parse_key_name` 會先轉小寫、只收英數底線：{unwritable}"
    shadowed = sorted(set(gui.KEY_ALIASES) & set(gui._LIBRARY_NAME_OVERRIDES))
    assert not shadowed, f"別名先查，這些覆寫會永遠輪不到：{shadowed}"
    both = sorted(set(gui._EXTRA_KEY_CODES) & set(gui._LIBRARY_NAME_OVERRIDES))
    assert not both, f"同一個名字不能同時是補充與覆寫：{both}"


# 本機載入的函式庫已經不再叫錯、但**套件庫的發佈版還是錯的**名字 → 理由。
#
# 兩個直譯器載入的是 `<AutoControlGUI 的本機 checkout>` 的原始碼樹（可編輯安裝），fresh clone 裝的卻是套件庫
# 那一份，所以「本機的表修好了」不等於「覆寫可以刪了」。2026-09-22 量過：原始碼樹（6002778）的 `down` 是
# 0x28，套件庫 0.0.222 仍是 0x80（F17）——那天照過期守門的訊息刪掉覆寫，每一個新 clone 的
# `hotkey down` 就會又按成 F17。發佈之後（在 fresh clone 環境量 `keyboard_keys_table["down"]`），
# 把覆寫與這裡那一筆一起刪掉。
_FIXED_UPSTREAM_AWAITING_RELEASE = {
    "down": "上游原始碼樹 2026-09-22 已改成 0x28（AutoControlGUI 6002778），套件庫 0.0.222 仍是 0x80",
}


def _stale_name_overrides(table, overrides):
    """`overrides` 裡**理由已經不成立**的名字（排序後）。

    覆寫的理由是「底層表有這個名字，但指向別的鍵碼」。兩種情況理由就沒了：底層
    把它改成跟覆寫一樣的鍵碼（上游修好了），或底層根本拿掉了這個名字（那它就不是
    覆寫，是補充，該搬去 `_EXTRA_KEY_CODES`）。
    """
    return sorted(name for name, code in overrides.items()
                  if name not in table or table[name] == code)


def test_every_library_name_override_is_still_needed(real_ac_tables):
    """覆寫表的每一筆，底層表都還是**叫錯**的——上游修好之後這支會紅。

    一個理由已經消失、卻還留在原地的例外，是本專案一再要防的形狀
    （`_OWNER_ONLY_SLASH` 的過期項目同理）：它不會壞任何東西，只是再也沒人知道
    它為什麼在那裡，下一個人也不敢刪。
    """
    keys, _ = real_ac_tables
    stale = [name for name in _stale_name_overrides(keys, gui._LIBRARY_NAME_OVERRIDES)
             if name not in _FIXED_UPSTREAM_AWAITING_RELEASE]
    assert not stale, (
        f"底層表已經不再叫錯這些名字：{stale}。上游修好了（或拿掉了那個名字），"
        "把它們從 `_gui_control._LIBRARY_NAME_OVERRIDES` 刪掉；若是底層拿掉了名字、"
        "而本專案仍要這個名字，改搬去 `_EXTRA_KEY_CODES`。")


def test_every_awaiting_release_entry_still_names_a_live_override():
    """等發佈清單的每一筆都要對應一筆還在的覆寫，而且寫了理由。

    覆寫刪掉之後這裡的那一筆就是一個永遠對不上的豁免，要一起刪；反過來，一筆豁免
    只要還在，就會讓上面那支過期守門對那個名字永遠閉嘴，所以它不能比覆寫活得久。"""
    orphans = sorted(set(_FIXED_UPSTREAM_AWAITING_RELEASE) - set(gui._LIBRARY_NAME_OVERRIDES))
    assert not orphans, f"這些名字已經沒有覆寫了，把等發佈清單裡那一筆也刪掉：{orphans}"
    blank = sorted(name for name, why in _FIXED_UPSTREAM_AWAITING_RELEASE.items() if not why.strip())
    assert not blank, f"等發佈清單的每一筆都要寫理由：{blank}"


def test_the_override_staleness_check_can_see_a_fix():
    """比對器自己的正面對照組：真表現在是乾淨的，把上面那支的斷言拿掉也不會紅，
    所以牙齒要長在這個純函式上。"""
    overrides = {"down": 0x28}
    assert _stale_name_overrides({"down": 0x80}, overrides) == []     # 還叫錯
    assert _stale_name_overrides({"down": 0x28}, overrides) == ["down"]  # 上游修好
    assert _stale_name_overrides({}, overrides) == ["down"]           # 上游拿掉
    assert _stale_name_overrides({"down": 0x80}, {}) == []


def test_every_code_the_library_can_name_can_also_be_written(real_ac_tables):
    """底層表叫得出名字的每一個鍵碼，本專案都要有一個**寫得出來**的名字。

    這是 `LAUNCH_APP2` 那一類的守門：名字只有大寫（或含 `parse_key_name` 不收的
    字元）的鍵，錄到了反查得出名字、存檔時卻被驗證擋掉。上游哪天再加一個這樣的
    名字，這支會點名它，要在 `_EXTRA_KEY_CODES` 補一個小寫的同碼名字。
    """
    keys, _ = real_ac_tables

    def writable(name):
        return name == name.lower() and bool(gui.HOTKEY_TOKEN_RE.match(name))

    # 被覆寫的名字送出去的是覆寫的鍵碼，所以它**不算**寫得出底層表裡那個錯鍵碼
    # （`down` 不算 0x80 的名字；0x80 靠的是 `f17`）。
    covered = {code for name, code in keys.items()
               if writable(name) and name not in gui._LIBRARY_NAME_OVERRIDES}
    covered |= {code for name, code in gui._EXTRA_KEY_CODES.items() if writable(name)}
    covered |= {code for name, code in gui._LIBRARY_NAME_OVERRIDES.items()
                if writable(name)}
    stranded = sorted({(code, name) for name, code in keys.items()
                       if code not in covered})
    assert not stranded, f"這些鍵碼只有寫不出來的名字：{stranded}"
    # 正面對照：真表裡確實有一個只有大寫名字的鍵（否則這支什麼都沒問到）。
    assert any(name != name.lower() and name.lower() not in keys
               for name in keys), "真表裡已經沒有只有大寫名字的鍵了，這支的前提要重看"


@pytest.mark.parametrize("name", _LAYOUT_LYING_NAMES)
def test_layout_dependent_keys_have_no_layout_lying_name(real_ac_tables,
                                                         monkeypatch, name):
    keys, _ = real_ac_tables

    class _TableOnly:
        keyboard_keys_table = keys

    monkeypatch.setattr(gui, "_AC", _TableOnly())
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    assert name not in gui.KEY_ALIASES and name not in gui._EXTRA_KEY_CODES
    with pytest.raises(GuiError):
        gui.parse_key_name(name)


def test_the_extra_names_pass_parse_key_name_without_the_backend(monkeypatch):
    """存檔驗證在沒有桌面的環境也要過（`parse_key_name` 的既有立場）。"""
    monkeypatch.setattr(gui, "_AC", None)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    for name in (*gui._EXTRA_KEY_CODES, *gui._LIBRARY_NAME_OVERRIDES):
        assert gui.parse_key_name(name.upper()) == name


def test_the_extra_names_are_found_by_reverse_lookup_without_the_backend(
        monkeypatch):
    """補充表是本專案自己的，底層載不進來也照樣反查得到（與 `parse_key_name`
    在沒有桌面時照樣收這些名字是同一個立場）。"""
    monkeypatch.setattr(gui, "_AC", None)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    assert gui._vk_to_name(0xBB) == "plus"
    assert gui._vk_to_name(0xBA) == "oem_1"
    assert gui._vk_to_name(0x41) is None        # 底層表的鍵沒有後端就查不到


def test_every_nameable_key_survives_record_validate_and_replay(key_library):
    """錄製 → 轉步驟 → 存檔驗證 → 重播，對 VK 1–254 裡**每一個叫得出名字的鍵**，
    三種修飾鍵各跑一次：一步都不能少，重播送出去的必須是錄到的那個鍵碼。
    """
    replayed: dict[int, int] = {}
    for modifier_vk, modifier_code in _HARD_MODIFIERS:
        for vk in range(1, 255):
            if vk in gui._MODIFIER_VK or gui._vk_to_name(vk) is None:
                continue
            events = [_event("kdown", 0.0, vk=modifier_vk),
                      _event("kdown", 0.01, vk=vk), _event("kup", 0.02, vk=vk),
                      _event("kup", 0.03, vk=modifier_vk)]
            steps = gui.record_to_steps(events, char_table={})
            assert len(steps) == 1 and steps[0].startswith("hotkey "), (vk, steps)
            # 錄出來的字面一律小寫（`_vk_to_name` 的偏好順序）：`ctrl+A` 讀起來像
            # 按了 shift，而 `LAUNCH_APP2` 與 `launch_app2` 是同一顆鍵的兩種寫法。
            assert steps[0] == steps[0].lower(), (vk, steps)
            for step in steps:
                gui.validate_macro_step(step)
            key_library.sent.clear()
            gui.run_macro(steps)
            assert key_library.sent == [("hotkey", [modifier_code, vk])], (
                vk, steps, key_library.raw[-1:])
            replayed[vk] = replayed.get(vk, 0) + 1
    # 正面對照：量到的是 152 個叫得出名字的鍵；補充表的每一個都要在裡面，而且三種
    # 修飾鍵都真的跑到。少了這幾句，`_vk_to_name` 一律回 None 會讓整支空轉通過。
    assert len(replayed) >= 150, len(replayed)
    assert set(gui._EXTRA_KEY_CODES.values()) <= set(replayed)
    assert set(gui._LIBRARY_NAME_OVERRIDES.values()) <= set(replayed)
    assert set(replayed.values()) == {len(_HARD_MODIFIERS)}


def test_a_hotkey_with_an_oem_key_validates_and_sends_its_keycode(key_library):
    gui.validate_macro_step("hotkey ctrl+oem_plus")
    assert gui.run_macro_step("hotkey ctrl+oem_plus") == "pressed control + oem_plus"
    assert gui.run_macro_step("hotkey ctrl+plus") == "pressed control + oem_plus"
    assert key_library.sent == [("hotkey", [17, 187]), ("hotkey", [17, 187])]
    # 交給函式庫的是整數，不是名字（函式庫的表查不到 `oem_plus`）。
    assert key_library.raw[0] == ("hotkey", ["control", 187])


def test_a_held_oem_key_is_registered_by_name_and_released_by_keycode(key_library):
    assert gui.run_macro_step("keydown oem_minus") == "holding oem_minus"
    assert ("key", "oem_minus") in gui._HELD_INPUTS
    assert [row[1] for row in gui.held_inputs()] == ["oem_minus"]
    assert gui.release_all_inputs() == ["oem_minus"]
    assert gui._HELD_INPUTS == {}
    assert key_library.raw == [("press", 189), ("release", 189)]
    assert gui.key_up("oem_minus") == "oem_minus"          # 沒按住也照樣送
    assert key_library.raw[-1] == ("release", 189)


def test_down_means_the_arrow_key_not_f17(key_library):
    """底層表的 `down` 是 0x80（＝ F17）；本專案的 `down` 一律送 0x28（方向鍵下）。"""
    assert gui.run_macro_step("hotkey down") == "pressed down"
    assert key_library.raw == [("hotkey", [0x28])]
    assert gui.run_macro_step("keydown down") == "holding down"
    assert ("key", "down") in gui._HELD_INPUTS
    assert gui.release_all_inputs() == ["down"]
    assert key_library.raw[1:] == [("press", 0x28), ("release", 0x28)]
    # F17 照樣寫得出來，而且真的是 F17。
    gui.run_macro_step("hotkey f17")
    assert key_library.sent[-1] == ("hotkey", [0x80])


def test_recording_the_arrow_key_writes_down_and_f17_stays_f17(key_library):
    def _record(vk):
        return gui.record_to_steps(
            [_event("kdown", 0.0, vk=0x11), _event("kdown", 0.01, vk=vk),
             _event("kup", 0.02, vk=vk), _event("kup", 0.03, vk=0x11)],
            char_table={})

    assert _record(0x28) == ["hotkey ctrl+down"]
    assert _record(0x80) == ["hotkey ctrl+f17"]
    key_library.sent.clear()
    gui.run_macro(_record(0x28) + _record(0x80))
    assert key_library.sent == [("hotkey", [17, 0x28]), ("hotkey", [17, 0x80])]


def test_an_overridden_name_never_names_its_wrong_library_code(monkeypatch):
    """合成表：被覆寫的名字在底層表裡指向 0x80，而它比 0x80 的其他名字都短——
    沒有排除規則的話，錄到 0x80 會寫成 `down`，重播時變成方向鍵。真表上 `f17`
    剛好比 `down` 短，所以只有合成資料看得出這條規則有沒有在。"""
    library = _KeyLibrary({"down": 0x80, "f17xx": 0x80, "vk_down": 0x28})
    monkeypatch.setattr(gui, "_AC", library)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    monkeypatch.setattr(gui, "_LIBRARY_NAME_OVERRIDES", {"down": 0x28})
    assert gui._vk_to_name(0x80) == "f17xx"
    assert gui._vk_to_name(0x28) == "down"


def test_an_override_name_does_not_depend_on_the_library_still_having_it(
        monkeypatch):
    """覆寫的名字是本專案的：上游哪天把 `down` 整個拿掉，它照樣寫得出來（過期
    守門會另外叫人把它搬去 `_EXTRA_KEY_CODES`，但在那之前不該先壞掉）。"""
    library = _KeyLibrary({"vk_down": 0x28, "f17": 0x80})
    monkeypatch.setattr(gui, "_AC", library)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    assert gui.parse_key_name("down") == "down"
    with pytest.raises(GuiError):
        gui.parse_key_name("definitely_not_a_key")


def test_an_override_name_wins_even_when_it_is_the_longer_name(monkeypatch):
    """合成表：覆寫的名字比底層那個名字長，照樣要勝出——覆寫表收的是一般人會打
    的名字，不能靠長短碰巧。真表上 `down` 剛好比 `vk_down` 短，看不出這條規則。"""
    library = _KeyLibrary({"dn": 0x28})
    monkeypatch.setattr(gui, "_AC", library)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    monkeypatch.setattr(gui, "_LIBRARY_NAME_OVERRIDES", {"arrowdown": 0x28})
    assert gui._vk_to_name(0x28) == "arrowdown"


def test_a_failing_hotkey_releases_the_oem_key_by_keycode(key_library):
    key_library.fail_press_at = 2
    with pytest.raises(GuiError):
        gui.press_hotkey(gui.parse_hotkey_tokens("ctrl+oem_2"))
    undone = [key for op, key in key_library.raw if op == "release"]
    assert undone == [0xBF, "control"], key_library.raw
    assert type(undone[0]) is int


def test_a_background_key_post_sends_the_oem_keycode(key_library, monkeypatch):
    monkeypatch.setattr(gui, "match_windows",
                        lambda needle: [(9, "Untitled - Editor")])
    assert gui.send_key_to_window("edit", "oem_2") == (
        9, "Untitled - Editor", "oem_2")
    assert key_library.raw == [("post", 0xBF)]


def test_paste_sends_keys_the_real_library_table_can_resolve(key_library,
                                                             monkeypatch):
    """`paste_text` 原本寫死 `["ctrl", "v"]`，而真表**沒有** `ctrl`——每一次貼上都
    在函式庫查表那一步失敗。其他測試的假後端什麼名字都收，所以看不出來。"""
    monkeypatch.setattr(gui, "set_clipboard", lambda _text: None)
    monkeypatch.setattr(gui.time, "sleep", lambda _s: None)
    gui.paste_text("測試")
    assert key_library.sent == [("hotkey", [17, 86])]


def test_a_key_that_still_has_no_name_is_logged_when_dropped(key_library, capsys):
    """VK 252（`VK_NONAME`）兩張表都沒有：照樣丟掉，但要在 log 留一行——原本完全
    安靜，錄製回報的步數少一步卻查不到原因。"""
    assert gui._vk_to_name(0xFC) is None
    events = [_event("kdown", 0.0, vk=0x11), _event("kdown", 0.01, vk=0xFC)]
    assert gui.record_to_steps(events, char_table={}) == []
    assert "vk=252" in capsys.readouterr().err


# 送鍵給函式庫的方法 → 鍵參數在第幾個位置。
_KEY_SENDING_METHODS = {
    "press_keyboard_key": 0, "release_keyboard_key": 0, "type_keyboard": 0,
    "check_key_is_press": 0, "hotkey": 0, "post_key_to_window": 1,
    "send_key_event_to_window": 1,
}
_KEY_WRAPPERS = ("_library_key", "_library_keys")


def _unwrapped_key_sends(tree):
    """回 `(送鍵呼叫點, 其中鍵參數沒包 `_library_key` 的)`，都是行號。"""
    import ast
    sites, unwrapped = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        index = _KEY_SENDING_METHODS.get(node.func.attr)
        if index is None:
            continue
        sites.append(node.lineno)
        arg = node.args[index] if len(node.args) > index else None
        if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id in _KEY_WRAPPERS):
            unwrapped.append(node.lineno)
    return sorted(sites), sorted(unwrapped)


def test_every_key_handed_to_the_library_goes_through_library_key():
    """送鍵給函式庫的每一處都要經過 `_library_key`——漏一處，補充表的名字在那
    條路上就會以函式庫查不到的字串送出去（放開路徑漏掉的話，卡住的鍵放不掉）。
    掃描而不是列舉，新增的送鍵點自動納入。"""
    import ast
    from pathlib import Path
    here = Path(__file__).resolve().parent.parent / "axiomatic"
    problems, total = [], 0
    for filename in ("_gui_control.py", "discord_bot.py"):
        tree = ast.parse((here / filename).read_text(encoding="utf-8"))
        sites, unwrapped = _unwrapped_key_sends(tree)
        total += len(sites)
        problems += [f"{filename}:{line}" for line in unwrapped]
    # 量到的是 7 處（`_gui_control` 裡）；少於這個數代表掃描自己壞了。
    assert total >= 7, total
    assert not problems, f"這些送鍵點沒有經過 `_library_key`：{problems}"


def test_the_key_send_scanner_can_tell_wrapped_from_unwrapped():
    import ast
    source = (
        "def f(ac, key, tokens, title):\n"
        "    ac.press_keyboard_key(_library_key(key))\n"
        "    ac.release_keyboard_key(key)\n"
        "    ac.hotkey(_library_keys(tokens))\n"
        "    ac.hotkey(tokens)\n"
        "    ac.post_key_to_window(title, _library_key(key))\n"
        "    ac.post_key_to_window(_library_key(title), key)\n"
        "    ac.release_keyboard_key(keycode=_library_key(key))\n"
        "    ac.press_keyboard_key(str(key))\n"
        "    ac.press_mouse(key)\n"
    )
    sites, unwrapped = _unwrapped_key_sends(ast.parse(source))
    assert sites == [2, 3, 4, 5, 6, 7, 8, 9]
    # 第 9 行：包了一層呼叫、但不是 `_library_key`——只看「是不是呼叫」的掃描會放過它。
    assert unwrapped == [3, 5, 7, 8, 9]


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))


# --------------------------------------------------------------------------
# 組合鍵送到一半失敗：不能把修飾鍵留在按下的狀態
#
# 底層 `je_auto_control.hotkey` 是「for 按下 → for 放開」而中間沒有 finally。
# 2026-08-30 實測（把底層 press/release 換成假的記錄器，不碰真實桌面）：三鍵組合
# 在第二個鍵按下時丟例外，呼叫序列只有兩次 press、**一次 release 都沒有**。
#
# 這條路徑繞過本模組的三層保險——那三層只掛在 `key_down` / `key_up` 上，組合鍵
# 全程不登記 `_HELD_INPUTS`，所以 `/input key status` 說沒東西按著、
# `/input key clear` 放不掉、也沒有逾時計時器。alt 卡住之後每個按鍵都變成選單
# 快捷鍵，而下指令的人不在電腦前面。
# --------------------------------------------------------------------------

class _HotkeyBackend:
    """記錄 press / release 呼叫序列的假後端；`fail_at` 指定第幾次 press 炸掉。"""

    def __init__(self, fail_at=None, release_fails=()):
        self.calls = []
        self.fail_at = fail_at
        self.release_fails = set(release_fails)
        self._presses = 0

    def hotkey(self, tokens, is_shift=False):
        # 與底層同構：先依序按下，再反向放開，中間沒有 finally。
        for token in tokens:
            self._presses += 1
            self.calls.append(("press", token))
            if self.fail_at is not None and self._presses == self.fail_at:
                raise OSError("SendInput failed")
        for token in reversed(tokens):
            self.calls.append(("release", token))
        return "", ""

    def release_keyboard_key(self, token, is_shift=False, skip_record=False):
        self.calls.append(("undo", token))
        if token in self.release_fails:
            raise OSError("SendInput failed")
        return str(token)


def _hotkey_env(monkeypatch, backend):
    monkeypatch.setattr(gui, "load_ac", lambda: backend)
    monkeypatch.setattr(gui, "input_desktop_available", lambda: True)


def test_a_hotkey_that_fails_midway_releases_what_it_pressed(monkeypatch):
    """三鍵組合在第二次按下時失敗 → 三個鍵都要被倒著放開一次。

    不去猜「失敗前按到第幾個」：放開一個沒按住的鍵在作業系統層是 no-op，全部倒著
    放一輪最安全。順序是反向的，跟按下的順序對稱。
    """
    backend = _HotkeyBackend(fail_at=2)
    _hotkey_env(monkeypatch, backend)
    with pytest.raises(GuiError):
        gui.press_hotkey(["ctrl", "shift", "a"])
    undone = [name for kind, name in backend.calls if kind == "undo"]
    assert undone == ["a", "shift", "ctrl"], (
        f"補放開的順序是 {undone}；應該是按下順序的反向。空的話代表 ctrl / shift "
        "留在按下的狀態——整台電腦會像壞掉一樣，而下指令的人不在電腦前面。")


def test_the_hotkey_failure_still_surfaces_and_says_the_keys_are_free(
        monkeypatch):
    """補放開之後**照樣**要丟 GuiError，而且訊息要讓人知道鍵盤沒卡住。

    順帶釘住 Secrecy：原始例外文字不得出現在使用者看得到的字串裡。
    """
    backend = _HotkeyBackend(fail_at=1)
    _hotkey_env(monkeypatch, backend)
    with pytest.raises(GuiError) as caught:
        gui.press_hotkey(["alt", "f4"])
    text = str(caught.value)
    assert "released" in text, (
        f"訊息沒說鍵已經放開了：{text!r}。使用者收到「組合鍵失敗」時最想知道的"
        "就是鍵盤現在是不是卡住的。")
    assert "SendInput" not in text, f"原始例外文字外洩：{text!r}"


def test_the_hotkey_undo_keeps_going_when_one_release_also_fails(monkeypatch):
    """補放開時其中一個又失敗，剩下的還是要試——重點是盡量把桌面解鎖。

    而且往上丟的必須是原本那個失敗，不是補救過程的失敗：把復原路徑的錯誤蓋到
    前面去，就再也看不出真正的原因了。
    """
    backend = _HotkeyBackend(fail_at=3, release_fails={"shift"})
    _hotkey_env(monkeypatch, backend)
    with pytest.raises(GuiError) as caught:
        gui.press_hotkey(["ctrl", "shift", "s"])
    undone = [name for kind, name in backend.calls if kind == "undo"]
    assert undone == ["s", "shift", "ctrl"], (
        f"其中一個放開失敗就停了：{undone}。ctrl 是最後一個、也是最該放掉的。")
    assert isinstance(caught.value.__cause__, OSError), (
        f"往上丟的原因變成 {caught.value.__cause__!r}——補救路徑的錯誤蓋掉了"
        "真正的失敗原因。")


def test_a_successful_hotkey_sends_no_extra_releases(monkeypatch):
    """成功時不得多補放開。

    底層自己已經反向放開過了；再送一輪會踩到「使用者在跑完之後自己按住同一個鍵」
    的情況，把他的鍵放掉——那正是 `release_added_since` 存在的理由。
    """
    backend = _HotkeyBackend()
    _hotkey_env(monkeypatch, backend)
    gui.press_hotkey(["ctrl", "c"])
    assert [kind for kind, _ in backend.calls].count("undo") == 0, (
        f"成功路徑補送了放開：{backend.calls}")
    assert backend.calls == [("press", "ctrl"), ("press", "c"),
                             ("release", "c"), ("release", "ctrl")]


def test_paste_inherits_the_hotkey_undo(monkeypatch):
    """`paste_text` 走的是同一個 `press_hotkey`，所以 ctrl 不會卡住。

    貼上是最常用的那條路（中日文只能靠剪貼簿），失敗機率不低而 ctrl 又是最糟的
    那顆鍵，所以明確釘一次而不是相信「反正它呼叫同一個函式」。
    """
    backend = _HotkeyBackend(fail_at=2)
    _hotkey_env(monkeypatch, backend)
    monkeypatch.setattr(gui, "set_clipboard", lambda _text: None)
    monkeypatch.setattr(gui.time, "sleep", lambda _s: None)
    with pytest.raises(GuiError):
        gui.paste_text("測試")
    undone = [name for kind, name in backend.calls if kind == "undo"]
    # 放開的是**函式庫認得的**名字 `control`：2026-09-21 以前這裡斷言 `ctrl`，
    # 也就是把「貼上送出一個函式庫查不到的鍵名」釘成了預期行為。
    assert undone == ["v", "control"], f"貼上失敗後沒放開 ctrl：{backend.calls}"


# ---------------------------------------------------------------------------
# `write()` 送到一半失敗時也要把鍵放開（2026-09-05）
#
# `press_hotkey` 2026-08-30 就補了防禦性的反向放開，`type_text` 一直沒有——而根因
# 是同一個：底層的 `type_keyboard` 是「按下 → 放開」中間**沒有 finally**，按下之後
# 放開那一步失敗，鍵就留在按下的狀態。
#
# **這一側比組合鍵更難自己恢復**：卡住的是一般字元鍵，Windows 會持續自動重複，
# 畫面上就是那個字被無限打出來；而本模組宣稱的三層保險（`_HELD_INPUTS` 登記、
# `release_all_inputs`、逾時自動放開）只掛在 `key_down`／`key_up` 上，這條路徑
# 完全繞過，所以 `/input key status` 會說什麼都沒按住、`/input key clear` 也放不掉。
# ---------------------------------------------------------------------------


class _WriteAC:
    """假的後端：只記錄呼叫，完全不碰真實鍵盤。"""
    keyboard_keys_table = {"a": 65, "b": 66, "A": 65, "1": 49,
                           "tab": 9, "return": 13, "space": 32}
    WRITE_CONTROL_KEYS = {"\n": "return", "\r": "return", "\t": "tab"}

    def __init__(self, fail=True):
        self.released = []
        self.fail = fail

    def release_keyboard_key(self, key):
        self.released.append(key)

    def write(self, text):
        if self.fail:
            raise RuntimeError("backend refused mid-string")


def test_a_failed_write_releases_the_keys_it_could_have_pressed(monkeypatch):
    ac = _WriteAC()
    monkeypatch.setattr(gui, "load_ac", lambda: ac)
    monkeypatch.setattr(gui, "_require_input_desktop", lambda: None)
    with pytest.raises(GuiError):
        gui.type_text("ab")
    assert ac.released == [65, 66], (
        "失敗後沒有把鍵放開——卡住的一般字元鍵會被 Windows 無限自動重複，"
        "而三層保險都掛在 key_down/key_up 上，這條路徑碰不到。")


def test_a_successful_write_releases_nothing(monkeypatch):
    """**反面**：正常成功時不可以亂送放開事件。"""
    ac = _WriteAC(fail=False)
    monkeypatch.setattr(gui, "load_ac", lambda: ac)
    monkeypatch.setattr(gui, "_require_input_desktop", lambda: None)
    gui.type_text("ab")
    assert ac.released == []


def test_control_characters_map_to_real_keys():
    """`write()` 把換行／Tab 轉成真正的按鍵，不是打出那個控制字元——回收也要對齊。"""
    ac = _WriteAC()
    assert gui.keys_a_write_could_press(ac, "a\tb\n") == [65, "tab", 66, "return"]


def test_characters_typed_via_unicode_are_not_listed():
    """表裡沒有的字元走 Unicode 事件，**不按任何鍵**，所以不該出現在回收清單裡。
    列進去雖然無害（放開沒按住的鍵是 no-op），但會讓清單與實際行為對不上。"""
    ac = _WriteAC()
    keys = gui.keys_a_write_could_press(ac, "a,b")   # ',' 不在表裡、也不是空白
    assert keys == [65, 66]


def test_whitespace_missing_from_the_table_is_released_as_space():
    """表裡沒有、但是空白的字元，`write()` 會按 `space`——回收清單也要有它，否則送到一半失敗時
    空白鍵可能一直按著（2026-09-23 分支覆蓋率：這一格從來沒跑過）。假後端的表裡沒有 `" "`。"""
    ac = _WriteAC()
    assert gui.keys_a_write_could_press(ac, "a b\u3000") == [65, "space", 66]


def test_the_list_is_deduplicated_and_capped():
    ac = _WriteAC()
    assert gui.keys_a_write_could_press(ac, "aaabbb") == [65, 66]
    # 上限要真的被踩到才測得出來——`"ab" * 500` 只有 **2 個相異字元**，上限拿掉
    # 也照樣通過（2026-09-05 變異測試實測存活）。這裡造一份相異鍵遠多於上限的表。
    class Wide:
        keyboard_keys_table = {chr(c): c for c in range(33, 33 + 200)}
        WRITE_CONTROL_KEYS = {}
    wide_text = "".join(chr(c) for c in range(33, 33 + 200))
    big = gui.keys_a_write_could_press(Wide(), wide_text)
    assert len(big) == gui._UNDO_WRITE_MAX_KEYS, (
        f"上限沒有生效（回了 {len(big)} 個鍵）——一段病態的長字串會把「補救」"
        "本身變成幾百次輸入事件。")


def test_the_undo_only_touches_keys_this_text_could_press():
    """與 `_undo_hotkey_press` 同一個立場：不做「順手把所有修飾鍵放掉」的大掃除
    ——使用者可能正握著 ctrl 站在鍵盤前面，替他放開是另一種靜默的錯。"""
    ac = _WriteAC()
    gui._undo_write_press(ac, "ab")
    for modifier in ("ctrl", "alt", "shift", "win"):
        assert modifier not in ac.released, (
            f"回收時順手放開了 {modifier}——那不是這次呼叫按下的鍵。")


def test_the_undo_survives_a_backend_that_cannot_release():
    """補救本身不可以再丟例外——那會蓋掉真正的失敗原因。"""
    class Hostile(_WriteAC):
        def release_keyboard_key(self, key):
            raise OSError("release failed")
    assert gui._undo_write_press(Hostile(), "ab") == []


def test_the_undo_survives_a_backend_with_no_key_table():
    class Bare:
        def release_keyboard_key(self, key):
            pass
    assert gui._undo_write_press(Bare(), "ab") == []


def test_both_typing_paths_now_have_an_undo():
    """`press_hotkey` 與 `type_text` 是同一個根因的兩個現場；補一邊漏一邊就是
    2026-08-30 到 09-05 之間的狀態。用 AST 釘住兩邊都有補救。"""
    import ast
    with open(gui.__file__, encoding="utf-8") as handle:
        src = handle.read()
    tree = ast.parse(src)
    for fname, undo in (("press_hotkey", "_undo_hotkey_press"),
                        ("type_text", "_undo_write_press")):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert undo in called, f"`{fname}` 失敗後沒有補一輪放開（少了 {undo}）"


# ==========================================================================
# `run_macro_step`：巨集的翻譯層
#
# 這支函式把一行文字翻成**真的打在這台機器上的鍵盤與滑鼠動作**，而它屬於
# `_OWNER_ONLY_GROUPS` 的 `macro` 群組——每一次執行都是擁有者授權的主機操作。
# 翻錯不會丟例外，它會**做錯事**：按錯鍵、點錯座標、放不開按著的鍵，而下指令的
# 人不在電腦前面。2026-09-08 之前這裡 101 個敘述句只有 20 個被執行過。
#
# 測試一律停在**翻譯層的邊界**：底層動作全部換成記錄器，斷言「叫了哪一個動作、
# 帶哪些參數、順序如何」。絕不越過那條線——這台機器上有一個開著的瀏覽器與一個
# 進行中的無人值守批次，一次真的按鍵就可能毀掉它。
# ==========================================================================
import inspect  # noqa: E402


def _never_abort() -> bool:
    """表格測試共用的中止回呼哨符（永遠不中止，但身分可以比對）。"""
    return False


# `run_macro_step` 會用到的底層動作。名字就是 `_gui_control` 的模組層函式，
# 呼叫是在執行當下才查全域，所以 monkeypatch 攔得到。
_MACRO_PRIMITIVES = (
    "mouse_click", "mouse_move", "mouse_drag", "mouse_scroll",
    "type_text", "paste_text", "press_hotkey",
    "key_down", "key_up", "release_all_inputs", "release_all_inputs_report",
    "window_focus", "window_close", "window_show",
    "wait_window", "wait_text", "click_text", "set_clipboard",
    "ui_click", "ui_wait", "wait_text_gone", "wait_window_gone",
    "wait_pixel", "_sleep_abortable",
)

# 每個記錄器的回傳值。刻意跟輸入**不一樣**，這樣「敘述用的是回傳值還是輸入」
# 分得出來——`wait_text` 該報後端真的找到的座標，不是步驟裡寫的那個。
_MACRO_SPY_RETURNS = {
    "key_down": lambda name: f"key:{name}",
    "key_up": lambda name: f"key:{name}",
    "release_all_inputs": lambda: ["shift", "control"],
    "release_all_inputs_report": lambda: (["shift", "control"], []),
    "window_focus": lambda needle: (0x1234, "某個視窗", 2),
    "window_close": lambda needle: (0x1234, "某個視窗", 2),
    "window_show": lambda needle, action: (0x1234, "某個視窗", 2),
    "wait_window": lambda *a, **k: (0x1234, "某個視窗"),
    "wait_text": lambda *a, **k: (11, 22),
    "click_text": lambda *a, **k: (33, 44),
    "ui_click": lambda *a, **k: (55, 66),
    "ui_wait": lambda *a, **k: {"x": 77, "y": 88, "name": "確定"},
    "wait_pixel": lambda *a, **k: (255, 0, 0),
}


class _MacroSpy:
    """把底層動作換成記錄器；記下 `(名稱, 位置參數, 關鍵字參數)`。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.originals: dict[str, object] = {}
        self.explode: dict[str, BaseException] = {}

    def reset(self) -> None:
        self.calls.clear()

    @property
    def names(self) -> list[str]:
        return [name for name, _a, _k in self.calls]

    def _make(self, name: str):
        def _recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.explode:
                raise self.explode[name]
            factory = _MACRO_SPY_RETURNS.get(name)
            return factory(*args, **kwargs) if factory else None
        return _recorder

    def install(self, monkeypatch) -> None:
        for name in _MACRO_PRIMITIVES:
            self.originals[name] = getattr(gui, name)
            monkeypatch.setattr(gui, name, self._make(name))


@pytest.fixture(name="macro_spy")
def _macro_spy_fixture(monkeypatch):
    spy = _MacroSpy()
    spy.install(monkeypatch)
    gui._HELD_INPUTS.clear()
    try:
        yield spy
    finally:
        gui._HELD_INPUTS.clear()


# --------------------------------------------------------------------------
# 性質 1：每一種 step 型別都翻譯成正確的後端呼叫（動作名、參數、順序）
#
# 表格拆成四張只是為了讀得下去；下面 `test_the_translation_table_covers_
# every_action_verb` 會把四張聯集起來跟 `MACRO_VERBS` 對，所以新增動詞卻沒補
# 案例的話，整組會紅。
# --------------------------------------------------------------------------
_POINTER_CASES = [
    ("click 100 200",
     ("mouse_click", ("mouse_left", 100, 200), {}), "clicked (100, 200)"),
    ("click 100 200 right",
     ("mouse_click", ("mouse_right", 100, 200), {}), "clicked (100, 200)"),
    # 虛擬桌面座標可以是負的（本機實測從 y = -164 起算）——翻譯層不能把它擋掉
    ("click -5 -164",
     ("mouse_click", ("mouse_left", -5, -164), {}), "clicked (-5, -164)"),
    # 雙擊沒有底層原語，靠 `times=2` 連點兩次；掉了這個關鍵字就變成單擊
    ("dclick 10 20",
     ("mouse_click", ("mouse_left", 10, 20), {"times": 2}), "double-clicked (10, 20)"),
    ("dclick 10 20 middle",
     ("mouse_click", ("mouse_middle", 10, 20), {"times": 2}), "double-clicked (10, 20)"),
    ("move 7 8", ("mouse_move", (7, 8), {}), "moved to (7, 8)"),
    ("drag 1 2 3 4",
     ("mouse_drag", (1, 2, 3, 4, "mouse_left"), {}), "dragged (1, 2) → (3, 4)"),
    ("drag 1 2 3 4 back",
     ("mouse_drag", (1, 2, 3, 4, "mouse_x1"), {}), "dragged (1, 2) → (3, 4)"),
    ("scroll 3", ("mouse_scroll", (3,), {}), "scrolled 3"),
    ("scroll -3 100 200", ("mouse_scroll", (-3, 100, 200), {}), "scrolled -3"),
]

_KEYBOARD_CASES = [
    ("type hello world", ("type_text", ("hello world",), {}), "typed 11 characters"),
    ("paste 中文 測試", ("paste_text", ("中文 測試",), {}), "pasted 5 characters"),
    ("hotkey ctrl+s",
     ("press_hotkey", (["control", "s"],), {}), "pressed control + s"),
    ("keydown shift", ("key_down", ("shift",), {}), "holding key:shift"),
    ("keyup ctrl", ("key_up", ("ctrl",), {}), "released key:ctrl"),
    ("release_keys", ("release_all_inputs_report", (), {}), "released all keys (2 released)"),
    ("clip set 你好 世界",
     ("set_clipboard", ("你好 世界",), {}), "wrote 5 characters to the clipboard"),
]

_WINDOW_CASES = [
    ("focus 記事本", ("window_focus", ("記事本",), {}), "focused window (2 matched)"),
    ("win close 記事本",
     ("window_close", ("記事本",), {}), "window close (2 matched)"),
    ("win min 記事本",
     ("window_show", ("記事本", "min"), {}), "window min (2 matched)"),
    # 動作大小寫不敏感，但視窗標題片段要原樣往下傳（含空白）
    ("win MAX 我的 視窗",
     ("window_show", ("我的 視窗", "max"), {}), "window max (2 matched)"),
]

_WAIT_CASES = [
    ("wait 3", ("_sleep_abortable", (3.0, _never_abort), {}), "waited 3 seconds"),
    ("wait 0.5", ("_sleep_abortable", (0.5, _never_abort), {}), "waited 0.5 seconds"),
    ("wait_window 儲存",
     ("wait_window", ("儲存", 15.0), {"should_abort": _never_abort}),
     "window appeared"),
    ("wait_window 30 儲存",
     ("wait_window", ("儲存", 30.0), {"should_abort": _never_abort}),
     "window appeared"),
    ("wait_text 完成",
     ("wait_text", ("完成", 15.0), {"should_abort": _never_abort}),
     "text appeared at (11, 22)"),
    ("wait_text 20 完成",
     ("wait_text", ("完成", 20.0), {"should_abort": _never_abort}),
     "text appeared at (11, 22)"),
    ("click_text 儲存", ("click_text", ("儲存",), {}), "clicked text at (33, 44)"),
    ("ui_click 開始", ("ui_click", ("開始",), {}), "clicked UI element at (55, 66)"),
    ("wait_ui 確定",
     ("ui_wait", ("確定", 15.0), {"should_abort": _never_abort}),
     "UI element appeared at (77, 88)"),
    ("wait_gone_text 處理中",
     ("wait_text_gone", ("處理中", 15.0), {"should_abort": _never_abort}),
     "text disappeared"),
    ("wait_gone_window 對話框",
     ("wait_window_gone", ("對話框", 15.0), {"should_abort": _never_abort}),
     "window closed"),
    ("wait_pixel 100 200 #ff0000",
     ("wait_pixel", (100, 200, (255, 0, 0), 15.0),
      {"should_abort": _never_abort}),
     "(100, 200) turned the specified colour"),
    ("wait_pixel 100 200 0,255,0 5",
     ("wait_pixel", (100, 200, (0, 255, 0), 5.0),
      {"should_abort": _never_abort}),
     "(100, 200) turned the specified colour"),
]

_ALL_SPY_CASES = (_POINTER_CASES + _KEYBOARD_CASES
                  + _WINDOW_CASES + _WAIT_CASES)

# 控制流動詞由 `run_macro_program` 直譯，不會走到 `run_macro_step` 的動作分支。
_CONTROL_VERBS = ({"repeat", "else", "end", "stop", "call"}
                  | set(gui.MACRO_CONDITIONS))


@pytest.mark.parametrize("step, expected, description",
                         _POINTER_CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_pointer_steps_translate_to_exactly_one_backend_call(
        macro_spy, step, expected, description):
    assert gui.run_macro_step(step, should_abort=_never_abort) == description
    assert macro_spy.calls == [expected]


@pytest.mark.parametrize("step, expected, description",
                         _KEYBOARD_CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_keyboard_steps_translate_to_exactly_one_backend_call(
        macro_spy, step, expected, description):
    assert gui.run_macro_step(step, should_abort=_never_abort) == description
    assert macro_spy.calls == [expected]


@pytest.mark.parametrize("step, expected, description",
                         _WINDOW_CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_window_steps_translate_to_exactly_one_backend_call(
        macro_spy, step, expected, description):
    assert gui.run_macro_step(step, should_abort=_never_abort) == description
    assert macro_spy.calls == [expected]


@pytest.mark.parametrize("step, expected, description",
                         _WAIT_CASES, ids=lambda v: v if isinstance(v, str) else "")
def test_wait_steps_translate_to_exactly_one_backend_call(
        macro_spy, step, expected, description):
    assert gui.run_macro_step(step, should_abort=_never_abort) == description
    assert macro_spy.calls == [expected]


def test_the_translation_table_covers_every_action_verb():
    """新增一個動作動詞卻沒補翻譯案例的話，這裡會紅。

    上面四張表是手寫的，手寫清單一定會過期；判準因此不是「表夠不夠長」而是
    「聯集等不等於 `MACRO_VERBS` 扣掉控制流」。
    """
    covered = {step.split()[0].lower() for step, _e, _d in _ALL_SPY_CASES}
    expected = set(gui.MACRO_VERBS) - _CONTROL_VERBS
    assert covered == expected, (
        f"沒有翻譯案例的動詞：{sorted(expected - covered)}；"
        f"表裡有但 MACRO_VERBS 沒有的：{sorted(covered - expected)}")


def test_click_text_deliberately_throws_the_leading_number_away():
    """`click_text 30 儲存` 的 `30` 會被丟掉——這是刻意的，不是漏接。

    `click_text` 是一次性的辨識，沒有等待迴圈可以套逾時；程式裡寫成
    `del timeout` 把這件事講明。釘住它是為了讓「哪天改成真的等 30 秒」變成一個
    有人決定過的改動，而不是重構時的副作用。
    """
    spy_free_step = "click_text 30 儲存"
    verb, args = gui.validate_macro_step(spy_free_step)
    assert verb == "click_text"
    timeout, target = gui.split_timeout(args, default=0.0)
    assert (timeout, target) == (30.0, "儲存")


def test_keydown_reports_the_resolved_key_not_the_typed_one(fake_ac):
    """敘述要說**實際按下去的那個鍵**。

    別名層把 `ctrl` 換成 `control`；如果敘述回吐使用者打的字，別名壞掉的時候
    報告仍然一片正常，看不出按下去的其實是別的鍵。
    """
    assert gui.run_macro_step("keydown ctrl") == "holding control"
    assert fake_ac.events == [("down", "control")]
    assert gui.run_macro_step("keyup ctrl") == "released control"


# --------------------------------------------------------------------------
# 性質 2：未知的動作要明確拒絕，不得安靜跳過
#
# 安靜跳過的後果是「巨集回報成功，但少做了一步」，而少做的那一步從外面完全看
# 不出來——使用者只會覺得「這個巨集有時候會失效」。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("step", [
    "fly 1 2",          # 根本不存在的動作
    "clik 100 200",     # 打錯字的 `click`
    "CLICKK 1 2",
    "sh Remove-Item -Recurse D:/",   # 刻意不在動詞表裡（存起來的遠端執行後門）
    "type",             # 認得動詞但少參數
    "release_keys now",  # 認得動詞但多參數
])
def test_a_step_that_is_not_a_real_action_is_rejected_not_skipped(
        macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == [], (
        f"`{step}` 被拒絕之前已經動了 {macro_spy.names}——拒絕必須發生在任何"
        "真實動作之前。")


@pytest.mark.parametrize("step", [
    "", "   ", "\t", "# 這是註解不是步驟",
])
def test_an_empty_or_blank_step_is_rejected(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


def test_a_none_step_is_rejected_instead_of_raising_a_type_error(macro_spy):
    """`None` 要收斂成 `GuiError`。

    呼叫端（`run_macro_program` → `cmd_macro`）只接 `GuiError`；漏出去的
    `AttributeError` 會變成「Ignoring exception in …」而使用者什麼都收不到。
    """
    with pytest.raises(GuiError):
        gui.run_macro_step(None, should_abort=_never_abort)
    assert macro_spy.calls == []


@pytest.mark.parametrize("step", [
    "stop", "repeat 3", "else", "end", "call other", "if_text 完成",
    "if_no_window 對話框", "if_pixel 1 1 #ffffff",
])
def test_a_control_verb_reaching_the_action_layer_is_rejected(macro_spy, step):
    """控制流動詞是 `run_macro_program` 的事；掉到這裡代表直譯器漏接了。

    這條路必須**丟例外**而不是安靜回一句「好了」：安靜回覆的話，直譯器少接一個
    控制動詞會表現成「`repeat` 沒有重複、但巨集回報成功」。
    """
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


def _verbs_dispatched_by(func_name: str) -> set[str]:
    """從 AST 推「這個函式接住了哪些動詞」，不看手寫名單。"""
    import ast
    with open(gui.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == func_name)
    found: set[str] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "verb"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant):
                found.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                found.update(elt.value for elt in comparator.elts
                             if isinstance(elt, ast.Constant))
            elif isinstance(comparator, ast.Name):
                # `verb in MACRO_CONDITIONS` 這種——查模組上那個容器
                container = getattr(gui, comparator.id, None)
                if isinstance(container, (dict, set, frozenset, tuple, list)):
                    found.update(container)
    return found


def test_no_macro_verb_falls_through_to_the_generic_reject():
    """`MACRO_VERBS` 裡的每一個動詞都要有人接。

    這是性質 2 的一般化。存檔時的驗證只問「動詞在不在表裡」，所以往
    `MACRO_VERBS` 加一個動詞、忘了補執行分支的話，巨集**存得起來**，然後在重播
    到那一行時才失敗——而前面的步驟已經在真實桌面上做過了，收不回來。

    「誰接住它」是從兩個函式的 AST 推出來的，不是第二張手寫清單（第二張清單只
    會跟第一張一起過期）。
    """
    dispatched = (_verbs_dispatched_by("run_macro_step")
                  | _verbs_dispatched_by("run_macro_program"))
    orphan = sorted(set(gui.MACRO_VERBS) - dispatched)
    assert not orphan, (
        f"這些動詞在 MACRO_VERBS 裡，但 `run_macro_step` 與 `run_macro_program` "
        f"都沒有分支接住：{orphan}")


def _verb_exemption_drift(exempt, interpreted) -> tuple:
    """`(列進豁免卻不是直譯的, 直譯了卻沒列進豁免的)`。

    純函式 ＋ 下面那支合成對照，是為了讓「把這個比較放鬆成單向」**殺得掉**。實測
    2026-09-11：本來寫成主測試裡一句 `==` 時，把它改回單向減法在 363 支測試上完全沒有
    症狀（兩個方向在真實資料上都是空的）。
    """
    extra = sorted(set(exempt) - set(interpreted))
    missing = sorted(set(interpreted) - set(exempt))
    return extra, missing


def test_the_verb_exemption_drift_comparison_actually_bites():
    """對照組：乾淨資料讓上面那句斷言兩個方向都是空的。"""
    assert _verb_exemption_drift({"a", "b"}, {"a", "b"}) == ([], [])
    # 豁免清單長胖——列進來的動詞從此不需要翻譯案例，這是真正會出事的方向。
    assert _verb_exemption_drift({"a", "b"}, {"a"}) == (["b"], [])
    # 直譯了卻沒列進豁免——會被要求一個它不該有的翻譯案例。
    assert _verb_exemption_drift({"a"}, {"a", "b"}) == ([], ["b"])
    # 兩邊各有一個，訊息要分得開。
    assert _verb_exemption_drift({"a", "x"}, {"a", "y"}) == (["x"], ["y"])


def test_the_control_verb_list_is_exactly_what_the_interpreter_handles():
    """`_CONTROL_VERBS` 是**豁免**清單，多一筆就等於少一支測試——而那是安靜的。

    `test_the_translation_table_covers_every_action_verb` 算的是
    `set(gui.MACRO_VERBS) - _CONTROL_VERBS`，所以列進豁免的動詞從此不需要任何翻譯
    案例（「這個動詞翻譯成哪一串後端呼叫」不再有人看）。那句 `==` 擋得住「把真的動作
    動詞列進來」，但擋不住**第三種形狀**：一個由 `run_macro_step` 接住的**新**動詞
    同時被列進 `_CONTROL_VERBS`。

    ⚠️ 實測 2026-09-11：把一個這樣的動詞（`hover`）塞進 `_CONTROL_VERBS`，
    `test_gui_control.py` 的 **362 支測試照樣全綠**。

    判準不是第二張手寫清單：`_verbs_dispatched_by` 已經能從 AST 推出直譯器真的接了
    哪些動詞（實測今天兩邊相等，13 對 13）。控制流動詞的定義就是「由
    `run_macro_program` 自己直譯、不往下送給動作層」，所以那個推導集合**就是**
    `_CONTROL_VERBS` 應該有的內容。
    """
    interpreted = _verbs_dispatched_by("run_macro_program")
    extra, missing = _verb_exemption_drift(_CONTROL_VERBS, interpreted)
    assert not extra, (
        f"`_CONTROL_VERBS` 多出來的：{extra}。這些動詞會永遠不需要翻譯案例"
        "——「它翻譯成哪一串後端呼叫」從此沒有人看，等於少了一支測試。")
    assert not missing, (
        f"`run_macro_program` 直譯了卻沒列進 `_CONTROL_VERBS` 的：{missing}。"
        "這些會被要求一個它不該有的翻譯案例（它們根本不會走到動作層）。")
    # 反面：掃描器真的看得到分支（掃壞掉的話上面那句會一直綠）
    assert "click" in _verbs_dispatched_by("run_macro_step")
    assert "repeat" in _verbs_dispatched_by("run_macro_program")


# --------------------------------------------------------------------------
# 性質 3：中途失敗時，按著的鍵／滑鼠鍵一定要放開
#
# 這是這支檔案已知的事故形態。卡住的不只是「這個巨集失敗了」——alt 卡住之後整台
# 電腦的每個按鍵都變成選單快捷鍵，而下指令的人不在現場。
# --------------------------------------------------------------------------
def test_a_failing_step_actually_sends_the_release_to_the_backend(fake_ac,
                                                                  monkeypatch):
    """不只登記表要清空，**放開事件要真的送出去**。

    只斷言 `held_inputs() == []` 的話，一個「清掉登記表但沒送放開」的實作照樣
    綠燈——而那正是最糟的組合：鍵實體上還按著，而 `/input key status` 說沒有。
    """
    _fail_hotkey_at_execution(monkeypatch)
    with pytest.raises(GuiError):
        gui.run_macro(["keydown shift", "hotkey ctrl+w"])
    assert ("down", "shift") in fake_ac.events, "前提：失敗之前 `keydown` 真的發生了"
    assert ("up", "shift") in fake_ac.events, (
        f"後端收到的事件是 {fake_ac.events}；少了 shift 的放開。")
    assert gui.held_inputs() == []


def test_a_failing_step_leaves_the_users_own_holds_alone(fake_ac, monkeypatch):
    """使用者跑巨集之前自己按著的鍵不能替他放開——那是他刻意留的狀態。"""
    gui.key_down("ctrl")
    fake_ac.events.clear()
    _fail_hotkey_at_execution(monkeypatch)
    with pytest.raises(GuiError):
        gui.run_macro(["keydown shift", "hotkey ctrl+w"])
    assert ("down", "shift") in fake_ac.events, "前提：失敗之前 `keydown` 真的發生了"
    assert ("up", "shift") in fake_ac.events
    assert ("up", "control") not in fake_ac.events
    assert [row[1] for row in gui.held_inputs()] == ["control"]


def test_an_aborted_wait_still_releases_what_the_macro_pressed(fake_ac,
                                                               monkeypatch):
    """被 `/macro stop` 中止跟失敗走不同的出口（`break` vs 例外），兩條都要放開。

    中止那條走的是 `run_macro_program` 迴圈裡的 `except GuiAborted: break`，
    不是 `finally` 前面的例外傳播——所以它是獨立的一條路，要獨立證明。
    """
    def _abort_the_wait(*_args, **_kwargs):
        raise gui.GuiAborted("停")

    monkeypatch.setattr(gui, "_sleep_abortable", _abort_the_wait)
    assert gui.run_macro(["keydown shift", "wait 5"]) == ["holding shift"]
    assert ("up", "shift") in fake_ac.events
    assert gui.held_inputs() == []


def test_a_step_that_blows_up_midway_still_releases_earlier_holds(fake_ac,
                                                                  monkeypatch):
    """底層丟的不是 `GuiError` 也一樣要放開。

    `finally` 接的是所有出口；如果哪天改成 `except GuiError:` 就只剩一半，而
    「非預期的例外」正是最可能在真實桌面上發生的那一種。
    """
    def _explode(*_args, **_kwargs):
        raise RuntimeError("底層爆了")

    monkeypatch.setattr(gui, "mouse_click", _explode)
    with pytest.raises(RuntimeError):
        gui.run_macro(["keydown shift", "click 1 2"])
    assert ("up", "shift") in fake_ac.events
    assert gui.held_inputs() == []


def test_the_macro_release_does_not_touch_the_mouse_buttons_it_never_pressed(
        fake_ac, monkeypatch):
    """滑鼠鍵也在同一個登記表裡，收尾一樣只放自己按的那些。"""
    gui.mouse_button_down("mouse_left")     # 使用者自己按著的
    fake_ac.events.clear()
    _fail_hotkey_at_execution(monkeypatch)
    with pytest.raises(GuiError):
        gui.run_macro(["keydown shift", "hotkey ctrl+w"])
    assert ("up", "shift") in fake_ac.events, "前提：收尾真的跑了"
    assert ("mup", "mouse_left") not in fake_ac.events
    assert [row[1] for row in gui.held_inputs()] == ["mouse_left"]


# --------------------------------------------------------------------------
# 性質 4：座標與數值參數的型別／範圍防護
#
# 巨集內容是**使用者提供的資料**（存在磁碟上、可以手改），所以這些值全部是不可
# 信輸入。`inf` / `nan` 這一條在本專案是有前科的：`_batch_config._is_finite_
# number` 記著同一個形狀——`x > 0` 這種檢查擋不住它們，因為 nan 的所有比較都是
# False。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("step", [
    "click nan 200", "click 100 nan", "move inf 1", "move 1 -inf",
    "drag 1 2 nan 4", "click 1.5 2", "click abc 1", "click 1e400 1",
    "click 99999999 1", "click 1 -99999999",
    "wait_pixel nan 1 #ffffff", "dclick 1 nan",
])
def test_a_coordinate_that_is_not_a_plain_integer_is_rejected(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == [], (
        f"`{step}` 已經送出 {macro_spy.names}——壞座標必須在動作之前就被擋下。")


@pytest.mark.parametrize("step, expected_xy", [
    ("click -5 -164", (-5, -164)),          # 副螢幕擺在主螢幕左邊／上面
    ("click 0 0", (0, 0)),
    ("click 65535 -65535", (65535, -65535)),  # 剛好在邊界上
])
def test_negative_and_boundary_coordinates_still_go_through(
        macro_spy, step, expected_xy):
    """過度收緊跟漏接一樣糟：釘住上限**內**的負座標必須通過。

    只測「壞值被擋」的話，把 `parse_coord` 改成「一律拒絕」也會全綠，而那會讓
    副螢幕上的每一個位置永遠點不到。
    """
    gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == [
        ("mouse_click", ("mouse_left",) + expected_xy, {})]


@pytest.mark.parametrize("step", [
    "scroll nan", "scroll inf", "scroll 1.5", "scroll abc", "scroll -inf",
])
def test_a_scroll_amount_that_is_not_an_integer_is_rejected(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


@pytest.mark.parametrize("step", [
    "wait_pixel 1 1 nope", "wait_pixel 1 1 #ff00", "wait_pixel 1 1 300,0,0",
    "wait_pixel 1 1 -1,0,0", "wait_pixel 1 1 1,2",
])
def test_a_bad_colour_is_rejected_before_anything_waits(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


@pytest.mark.parametrize("step", [
    "hotkey +", "hotkey ctrl+;", "hotkey 不存在的鍵", "hotkey ctrl+s+不存在",
    "keydown ctrl+s", "keyup 不存在的鍵", "keydown 1+1",
])
def test_a_bad_key_name_never_reaches_the_backend(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


def test_a_trailing_plus_in_a_hotkey_is_tolerated_not_treated_as_a_key(
        macro_spy):
    """`hotkey ctrl+` 是**合法**的——空的那一段會被濾掉，剩一顆 ctrl。

    釘住它是因為上面那張壞值表很容易順手把它加進去，然後為了讓測試綠而把
    `parse_hotkey_tokens` 收緊——那會讓「按住單一修飾鍵」這個用法消失。
    """
    assert gui.run_macro_step("hotkey ctrl+",
                              should_abort=_never_abort) == "pressed control"
    assert macro_spy.calls == [("press_hotkey", (["control"],), {})]


# --------------------------------------------------------------------------
# 性質 5：不得有無界的等待或重試
#
# 2026-09-08 實測到的真缺陷：`parse_duration` 用 `value < 0` 與 `value > maximum`
# 兩道範圍檢查擋秒數，而 **nan 兩道都過**（nan 的比較恆為 False）。放行的後果不是
# 「等很久」是**永遠不會結束**——每個等待迴圈都是
# `deadline = monotonic() + timeout` 配 `if monotonic() >= deadline`，而
# `x >= nan` 恆為 False。當時實測 `wait_window(…, nan)` 輪詢四十次仍未逾時。
# 走 bot 那一側更糟：`/win wait`、`/locate text wait`、`/locate ui wait` 都是
# `asyncio.to_thread` 且不帶中止回呼，卡住的執行緒沒有人收得回來。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("step", [
    "wait nan", "wait inf", "wait -inf", "wait 1e400",
    "wait_window nan 儲存", "wait_text nan 完成", "wait_ui nan 確定",
    "wait_gone_text nan 處理中", "wait_gone_window nan 對話框",
    "wait_pixel 1 1 #ffffff nan",
])
def test_a_non_finite_duration_cannot_create_an_unbounded_wait(
        macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == [], (
        f"`{step}` 已經把 {macro_spy.names} 送出去了——nan 逾時進到等待迴圈就"
        "再也不會逾時。")


def test_a_non_finite_duration_is_rejected_at_save_time_too(macro_dir):
    """存檔驗證要一起擋，不然壞巨集會存得起來、跑到那一行才卡住。"""
    del macro_dir
    with pytest.raises(GuiError):
        gui.parse_macro_steps("wait_text nan 完成")
    with pytest.raises(GuiError):
        gui.parse_macro_steps("wait nan")


def test_the_wait_loops_would_never_time_out_on_a_nan_deadline():
    """把缺陷的**機制**釘起來，而不只是釘住現在擋得住。

    這一句就是所有等待迴圈逾時判斷的形狀。它對 nan 恆為 False，所以只要 nan
    進得去，逾時那一行就永遠不成立——`parse_duration` 因此必須是有限值的閘。
    """
    assert (1.0 >= float("nan")) is False
    assert (float("nan") < 0) is False
    assert (float("nan") > 120.0) is False
    with pytest.raises(GuiError):
        gui.parse_duration("nan", maximum=120.0)


@pytest.mark.parametrize("step", [
    "wait 121", "wait_window 999 儲存", "wait_text 121 完成",
    "wait_ui 500 確定", "wait_pixel 1 1 #ffffff 121", "wait -1",
])
def test_every_duration_argument_is_capped_and_non_negative(macro_spy, step):
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


def test_every_waiting_step_hands_the_abort_callback_down(macro_spy):
    """會等的步驟一定要把中止回呼傳下去。

    判準是**機制**不是手寫名單：底層動作的簽名裡有 `should_abort` 就必須收到
    它。新增一個會等的動詞卻忘了轉交的話，這裡會紅——而在真實世界那個症狀是
    「`/macro stop` 按了沒反應」，最久要等 `MACRO_MAX_WAIT_SEC`（120 秒）。
    """
    checked = 0
    for step, (name, _args, _kwargs), _desc in _ALL_SPY_CASES:
        macro_spy.reset()
        gui.run_macro_step(step, should_abort=_never_abort)
        params = inspect.signature(macro_spy.originals[name]).parameters
        if "should_abort" not in params:
            continue
        checked += 1
        _got_name, got_args, got_kwargs = macro_spy.calls[-1]
        forwarded = (got_kwargs.get("should_abort") is _never_abort
                     or _never_abort in got_args)
        assert forwarded, (
            f"`{step}` 呼叫 {name} 時沒有轉交中止回呼："
            f"args={got_args!r} kwargs={got_kwargs!r}")
    assert checked >= 7, (
        f"只檢查到 {checked} 個會等的步驟——簽名探測大概壞了，"
        "空集合會讓這支測試永遠是綠的。")


def test_a_step_with_no_abort_callback_still_reaches_the_backend(macro_spy):
    """`should_abort` 是選用的；沒給的時候不能整個罷工。"""
    gui.run_macro_step("wait_text 完成")
    assert macro_spy.calls == [
        ("wait_text", ("完成", 15.0), {"should_abort": None})]


# --------------------------------------------------------------------------
# 性質 5b：存檔驗證對每個參數的解讀，必須跟執行端一模一樣
#
# 2026-09-21 實測到的兩個缺口（差分探針：驗證端放行的 121,679 個步驟裡，有六個
# 動詞在執行時才因為**參數解讀**而丟例外）：
#
# * `if_pixel` / `if_no_pixel` 的第四個參數，驗證端當秒數、執行端當整數容差。
#   `… 2.5` 存得起來、重播時丟出沒有理由的錯誤；`… 150` 這個合法容差反而在存檔
#   時被「秒數上限」擋掉。
# * `wait_ui` / `wait_gone_text` / `wait_gone_window` / `click_text` 開頭的逾時，
#   驗證端完全沒看，執行端卻經過 `split_timeout` 解析。
#
# 修法是兩邊呼叫同一支解析器（`parse_tolerance`、`split_timeout` ＋
# `MACRO_TIMEOUT_DEFAULTS`），這裡釘住「擋」與「放」兩個方向。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("0", 0), ("12", 12), ("150", 150), ("255", 255), (" 7 ", 7),
])
def test_parse_tolerance_accepts_the_whole_channel_range(raw, expected):
    assert gui.parse_tolerance(raw) == expected


@pytest.mark.parametrize("raw", [
    "-1", "256", "2.5", "0.0", "1e1", "nan", "inf", "", "abc",
    True, False, None,
])
def test_parse_tolerance_rejects_anything_but_an_integer_in_range(raw):
    """兩個布林都要擋：`True` 是 `int` 的子類別，最容易從整數檢查的縫溜過去。"""
    with pytest.raises(GuiError):
        gui.parse_tolerance(raw)


@pytest.mark.parametrize("step", [
    "if_pixel 10 10 #ffffff 2.5",
    "if_pixel 10 10 #ffffff 0.0",
    "if_pixel 10 10 #ffffff 1e1",
    "if_pixel 10 10 #ffffff -1",
    "if_pixel 10 10 #ffffff 256",
    "if_no_pixel 10 10 #ffffff 2.5",
    "if_no_pixel 10 10 #ffffff 256",
])
def test_a_pixel_tolerance_the_evaluator_cannot_read_is_rejected_at_save_time(
        monkeypatch, step):
    with pytest.raises(GuiError) as excinfo:
        gui.parse_macro_steps(f"wait 0\n{step}\nend")
    message = str(excinfo.value)
    assert "Line 2" in message and "tolerance" in message, message
    # 同一個值交給執行端也必須是 `GuiError`（有理由的拒絕），不是裸的
    # `ValueError`——後者會被 bot 的寬 `except` 折成一句沒有原因的「巨集執行失敗」。
    monkeypatch.setattr(gui, "pixel_color", lambda x, y: (255, 255, 255))
    verb, *args = step.split()
    with pytest.raises(GuiError):
        gui.eval_macro_condition(verb, args)


@pytest.mark.parametrize("step, current, expected", [
    # 150／255 以前在存檔時被「秒數上限是 120 秒」擋掉
    ("if_pixel 10 10 #ffffff 150", (120, 120, 120), True),
    ("if_pixel 10 10 #ffffff 150", (100, 255, 255), False),
    ("if_pixel 10 10 #ffffff 255", (0, 0, 0), True),
    ("if_pixel 10 10 #ffffff 0", (255, 255, 255), True),
    ("if_pixel 10 10 #ffffff 0", (254, 255, 255), False),
    # 沒給就是預設的 12
    ("if_pixel 10 10 #ffffff", (243, 243, 243), True),
    ("if_pixel 10 10 #ffffff", (242, 255, 255), False),
    ("if_no_pixel 10 10 #ffffff 150", (120, 120, 120), False),
])
def test_a_legitimate_pixel_tolerance_is_saved_and_evaluated(
        monkeypatch, step, current, expected):
    assert gui.parse_macro_steps(f"{step}\nend") == [step, "end"]
    asked = []

    def _pixel(x, y):
        asked.append((x, y))
        return current

    monkeypatch.setattr(gui, "pixel_color", _pixel)
    verb, args = gui.validate_macro_step(step)
    assert gui.eval_macro_condition(verb, args) is expected
    assert asked == [(10, 10)]


def test_a_pixel_tolerance_reaches_the_interpreter_intact(monkeypatch):
    """從直譯器一路走下去，確認存得起來的容差在重播時也真的被採用。"""
    monkeypatch.setattr(gui, "pixel_color", lambda x, y: (120, 120, 120))
    ran = []
    gui.run_macro_program(
        ["if_pixel 10 10 #ffffff 150", "wait 0", "else", "wait 0.01", "end"],
        on_step=lambda i, s, d: ran.append(s))
    assert ran == ["wait 0"]


def test_wait_pixel_keeps_its_fourth_argument_as_a_timeout(macro_spy):
    """`wait_pixel` 的第四個參數**才是**秒數，拆分支之後不能被順手改成容差。"""
    gui.validate_macro_step("wait_pixel 10 10 #ffffff 2.5")
    with pytest.raises(GuiError) as excinfo:
        gui.validate_macro_step("wait_pixel 10 10 #ffffff 150")
    assert "seconds" in str(excinfo.value)
    gui.run_macro_step("wait_pixel 10 10 #ffffff 2.5",
                       should_abort=_never_abort)
    assert macro_spy.calls == [
        ("wait_pixel", (10, 10, (255, 255, 255), 2.5),
         {"should_abort": _never_abort})]


# 刻意手寫，不從 `MACRO_TIMEOUT_DEFAULTS` 推：從表推的話，表裡少一個動詞，這組
# 測試就跟著少一格，驗證端漏看那個動詞也不會紅。
_LEADING_TIMEOUT_VERBS = (
    "wait_window", "wait_text", "click_text",
    "wait_ui", "wait_gone_text", "wait_gone_window",
)


@pytest.mark.parametrize("verb", _LEADING_TIMEOUT_VERBS)
@pytest.mark.parametrize("bad", ["150", "121", "-1", "nan", "inf", "1e400"])
def test_every_leading_timeout_the_executor_parses_is_rejected_at_save_time(
        macro_spy, verb, bad):
    step = f"{verb} {bad} 目標"
    with pytest.raises(GuiError):
        gui.validate_macro_step(step)
    with pytest.raises(GuiError) as excinfo:
        gui.parse_macro_steps(f"wait 0\n{step}")
    assert "Line 2" in str(excinfo.value)
    # 執行端對同一個值也拒絕，而且拒絕發生在任何真實動作之前
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)
    assert macro_spy.calls == []


@pytest.mark.parametrize("verb", _LEADING_TIMEOUT_VERBS)
@pytest.mark.parametrize("args", [
    "0 目標", "120 目標", "2.5 目標",
    # 只有一個參數時它就是目標本身（等畫面上出現「150」），不是逾時
    "150",
    # 數字不在開頭就不是逾時
    "目標 150",
])
def test_a_leading_timeout_within_range_or_a_numeric_target_still_saves(
        verb, args):
    step = f"{verb} {args}"
    assert gui.parse_macro_steps(step) == [step]


def test_the_leading_timeout_table_names_exactly_the_verbs_that_take_one():
    """表與手寫清單對帳，兩個方向都要。

    表多一個動詞：驗證端會對一個根本不讀逾時的動詞做秒數檢查，把合法步驟擋掉。
    表少一個動詞：執行端 `MACRO_TIMEOUT_DEFAULTS[verb]` 取不到預設值。
    """
    assert set(gui.MACRO_TIMEOUT_DEFAULTS) == set(_LEADING_TIMEOUT_VERBS)


# --------------------------------------------------------------------------
# 性質 6：回報要分得出「做完了」與「沒做成」
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "mouse_click", "mouse_move", "mouse_drag", "mouse_scroll", "type_text",
    "paste_text", "press_hotkey", "window_focus", "wait_text", "wait_pixel",
    "ui_click", "set_clipboard",
])
def test_a_failing_backend_raises_instead_of_returning_a_success_line(
        macro_spy, name):
    """底層失敗時不得回一句敘述——回了就等於把失敗長成成功。

    `run_macro_program` 只靠「有沒有丟例外」判斷這一步成不成；吞掉例外再回一句
    「已點選」，使用者會看到一份**每一步都成功**的報告，而桌面上什麼都沒發生。
    """
    step = {
        "mouse_click": "click 1 2", "mouse_move": "move 1 2",
        "mouse_drag": "drag 1 2 3 4", "mouse_scroll": "scroll 1",
        "type_text": "type x", "paste_text": "paste x",
        "press_hotkey": "hotkey ctrl+s", "window_focus": "focus 記事本",
        "wait_text": "wait_text 完成", "wait_pixel": "wait_pixel 1 1 #ffffff",
        "ui_click": "ui_click 開始", "set_clipboard": "clip set x",
    }[name]
    macro_spy.explode[name] = GuiError("底層失敗")
    with pytest.raises(GuiError):
        gui.run_macro_step(step, should_abort=_never_abort)


@pytest.mark.parametrize("step, expected", [
    ("wait_text 完成", "text appeared at (11, 22)"),
    ("click_text 儲存", "clicked text at (33, 44)"),
    ("ui_click 開始", "clicked UI element at (55, 66)"),
    ("wait_ui 確定", "UI element appeared at (77, 88)"),
    ("focus 記事本", "focused window (2 matched)"),
])
def test_the_report_describes_the_outcome_not_the_request(macro_spy, step,
                                                          expected):
    """報告要說**後端真的找到什麼**，不是步驟裡寫了什麼。

    記錄器刻意回跟輸入無關的數字：如果敘述改成回吐輸入，這裡就會紅。回吐輸入的
    版本在定位失準時仍然一片正常，看不出點到的其實是別的地方。
    """
    assert gui.run_macro_step(step, should_abort=_never_abort) == expected


@pytest.mark.parametrize("step, secret", [
    ("type 我的密碼是 hunter2", "hunter2"),
    ("paste D:/Work/Example/auth.md", "auth.md"),
    ("clip set sk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
])
def test_the_report_says_how_much_was_typed_not_what(macro_spy, step, secret):
    """輸入內容不得出現在敘述裡——那句敘述會被貼到聊天平台上。

    巨集打的可能是密碼、token 或主機路徑（保密規則 Layer 1）。現在三個動詞都只
    報字數；哪天有人為了「訊息更好懂」把內容加進去，這裡會紅。
    """
    detail = gui.run_macro_step(step, should_abort=_never_abort)
    assert secret not in detail
    assert "characters" in detail


def test_the_window_report_says_how_many_matched_not_which(macro_spy):
    """視窗標題幾乎一定帶絕對路徑（編輯器／IDE），所以只報命中數量。"""
    for step in ("focus 記事本", "win close 記事本", "win min 記事本"):
        macro_spy.reset()
        detail = gui.run_macro_step(step, should_abort=_never_abort)
        assert "某個視窗" not in detail, (
            f"`{step}` 的回報帶出了後端給的視窗標題：{detail}")
        assert "2 matched" in detail


def test_a_successful_step_always_returns_a_non_empty_description(macro_spy):
    """每一種動作都要回一句話——空字串在 `!macro run` 的逐步回報裡看起來像沒跑。"""
    for step, _expected, _desc in _ALL_SPY_CASES:
        macro_spy.reset()
        detail = gui.run_macro_step(step, should_abort=_never_abort)
        assert isinstance(detail, str) and detail.strip(), (
            f"`{step}` 回了 {detail!r}")


# --------------------------------------------------------------------------
# 拖曳：插值座標與「失敗一定要放開」
#
# 這兩件事在真的桌面上才看得到後果——座標算錯是拖錯東西，沒放開是整台電腦像
# 壞掉一樣（滑鼠鍵卡在按下狀態，而下指令的人不在電腦前面）。所以全部用假後端
# 驗呼叫序列，一個真的滑鼠事件都不送。
# --------------------------------------------------------------------------
class _FakeClock:
    """只在 `sleep` 時前進的假時鐘：測試不必真的等，影格數也才算得準。"""

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += max(0.0, float(secs))


class _FakeDragAC:
    """記錄滑鼠呼叫序列的假後端。`fail_at_move` 指定第幾次移動要炸。"""

    def __init__(self, *, fail_at_move=None, release_raises=False,
                 desktop=True):
        self.calls: list[tuple] = []
        self.moves: list[tuple[int, int]] = []
        self.fail_at_move = fail_at_move
        self.release_raises = release_raises
        self.desktop = desktop

    def input_desktop_available(self):
        return self.desktop

    def set_mouse_position(self, x, y):
        self.calls.append(("move", x, y))
        self.moves.append((x, y))
        if self.fail_at_move is not None and len(self.moves) == self.fail_at_move:
            raise RuntimeError("backend blew up mid-drag")

    def press_mouse(self, button, x=None, y=None):
        self.calls.append(("press", button, x, y))

    def release_mouse(self, button, x=None, y=None):
        self.calls.append(("release", button, x, y))
        if self.release_raises:
            raise RuntimeError("release also failed")


@pytest.fixture(name="drag_ac")
def _drag_ac_fixture(monkeypatch):
    """假後端 ＋ 假時鐘。`_AC` 直接換掉，所以連 `input_desktop_available` 的
    真實路徑都走得到（不必把 `_require_input_desktop` 整個 patch 掉）。"""
    fake = _FakeDragAC()
    clock = _FakeClock()
    monkeypatch.setattr(gui, "_AC", fake)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    monkeypatch.setattr(gui, "time", clock)
    fake.clock = clock
    return fake


def test_drag_presses_at_the_start_and_releases_at_the_end(drag_ac):
    gui.mouse_drag(10, 20, 110, 220, steps=4, settle=0.0)
    kinds = [c[0] for c in drag_ac.calls]
    assert kinds == ["move", "press"] + ["move"] * 4 + ["release"], (
        f"呼叫序列不對：{drag_ac.calls}")
    assert drag_ac.calls[0] == ("move", 10, 20), "沒有先把游標移到起點"
    assert drag_ac.calls[1] == ("press", "mouse_left", 10, 20)
    assert drag_ac.calls[-1] == ("release", "mouse_left", 110, 220)


def test_drag_interpolates_every_intermediate_position(drag_ac):
    """一次瞬移會被多數應用程式當成「按下又放開」，所以中間那幾步是功能本身，
    不是裝飾。距離刻意選成除不盡（100/3），把 `int()` 的截斷也一起釘住。"""
    gui.mouse_drag(0, 0, 100, 50, steps=3, settle=0.0)
    assert drag_ac.moves == [(0, 0), (33, 16), (66, 33), (100, 50)]


def test_the_last_interpolated_step_lands_exactly_on_the_target(drag_ac):
    """最後一步必須**剛好**是終點：差一個像素就是放在隔壁的檔案上。

    `index` 跑到 `steps` 時比例是 1.0，所以是精確的——但那要 `range(1, steps+1)`
    才成立。寫成 `range(steps)` 會停在倒數第二格，而畫面上幾乎看不出來。
    """
    for steps in (1, 2, 7, 24, 60):
        drag_ac.calls.clear()
        drag_ac.moves.clear()
        gui.mouse_drag(3, 7, 1234, -567, steps=steps, settle=0.0)
        assert drag_ac.moves[-1] == (1234, -567), (
            f"steps={steps} 時最後一步停在 {drag_ac.moves[-1]}")
        assert len(drag_ac.moves) == steps + 1, "起點那一次移動不見了"


def test_drag_handles_negative_coordinates_for_a_secondary_monitor(drag_ac):
    """虛擬桌面原點可以是負的（本機實測 (0, -164)），插值不得夾到 0。"""
    gui.mouse_drag(-300, -164, -100, -64, steps=2, settle=0.0)
    assert drag_ac.moves == [(-300, -164), (-200, -114), (-100, -64)]


def test_a_drag_that_goes_nowhere_still_presses_and_releases(drag_ac):
    """起點＝終點：有些應用程式靠「按住不動」開選單，不能因為位移是 0 就跳過。"""
    gui.mouse_drag(50, 50, 50, 50, steps=3, settle=0.0)
    assert [c[0] for c in drag_ac.calls].count("press") == 1
    assert [c[0] for c in drag_ac.calls].count("release") == 1
    assert set(drag_ac.moves) == {(50, 50)}


def test_a_failed_drag_releases_the_button_before_reporting(drag_ac,
                                                            monkeypatch):
    """補救碼一直都在，但沒有測試。卡住的是滑鼠**左鍵**——之後每一次點選都會被
    當成框選或拖曳，整個桌面等於被鎖住，而人不在電腦前面。"""
    monkeypatch.setattr(gui, "_AC", _FakeDragAC(fail_at_move=3))
    fake = gui._AC
    with pytest.raises(GuiError) as caught:
        gui.mouse_drag(0, 0, 100, 100, steps=8, settle=0.0)
    assert [c[0] for c in fake.calls].count("release") == 1, (
        f"失敗之後沒有放開滑鼠鍵：{fake.calls}")
    assert fake.calls[-1] == ("release", "mouse_left", 100, 100)
    assert isinstance(caught.value.__cause__, RuntimeError), (
        "補救把原始錯誤蓋掉了——`__cause__` 才是查得下去的那個")


def test_a_successful_drag_does_not_release_twice(drag_ac):
    """反面：成功時不得多放開一次。

    這一半跟上一半必須成對存在。只驗「失敗會放開」的話，把補救碼搬到 `finally`
    也是綠的——而那會在成功路徑上多送一次 release，某些應用程式會讀成第二次點選。
    """
    gui.mouse_drag(0, 0, 10, 10, steps=3, settle=0.0)
    assert [c[0] for c in drag_ac.calls].count("release") == 1


def test_a_release_that_also_fails_does_not_mask_the_original_error(monkeypatch):
    """補救本身失敗時要吞掉自己的例外——原始錯誤比「放開也失敗了」有用得多。"""
    fake = _FakeDragAC(fail_at_move=2, release_raises=True)
    monkeypatch.setattr(gui, "_AC", fake)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    monkeypatch.setattr(gui, "time", _FakeClock())
    with pytest.raises(GuiError) as caught:
        gui.mouse_drag(0, 0, 5, 5, steps=4, settle=0.0)
    assert "drag" in str(caught.value)
    assert str(caught.value.__cause__) == "backend blew up mid-drag"


def test_a_locked_desktop_refuses_the_drag_before_touching_the_mouse(monkeypatch):
    """鎖定時送出去的事件會被安靜丟掉，使用者卻收到「已拖曳」。要在按下之前就擋。"""
    fake = _FakeDragAC(desktop=False)
    monkeypatch.setattr(gui, "_AC", fake)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    monkeypatch.setattr(gui, "time", _FakeClock())
    with pytest.raises(GuiError):
        gui.mouse_drag(0, 0, 10, 10, steps=2, settle=0.0)
    assert fake.calls == [], f"鎖定狀態下還是動了滑鼠：{fake.calls}"


def test_the_drag_report_does_not_leak_the_backend_error(drag_ac, monkeypatch):
    """對外訊息維持泛用——原始例外文字可能夾帶主機路徑。"""
    monkeypatch.setattr(gui, "_AC", _FakeDragAC(fail_at_move=1))
    with pytest.raises(GuiError) as caught:
        gui.mouse_drag(0, 0, 9, 9, steps=3, settle=0.0)
    assert "blew up" not in str(caught.value)


# --------------------------------------------------------------------------
# 連拍 GIF：三個上限，以及「夾擠對 nan 安全」的反例
#
# `capture_gif` 的兩道夾擠都寫成 `max(常數, min(值, 上限))`——**常數在前**，所以
# nan 會在第一個 `max` 就被丟掉。這跟 `_dorossi_clamp_usage_wait` 舊版
# （`max(值, 常數)`，變數在前，nan 一路穿過去）剛好是同一個機制的兩面。這裡的
# 測試存在的理由就是：有人把它「順手整理」成 `min(max(...))` 不會有任何症狀，
# 只有把行為釘住才擋得到。
# --------------------------------------------------------------------------
class _FakeFrames:
    """假的截圖來源：回真的 PIL 影像（不碰螢幕），並且**有次數上限**。

    上限是刻意的。若 fps 的夾擠被改壞成 nan，`interval` 會是 nan，`rest > 0`
    永遠是 False，於是迴圈完全不睡——配上只在 sleep 前進的假時鐘就是**無限迴圈**。
    掛住的測試比紅的測試更糟，所以在這裡把它變成一個有界的失敗。
    """

    LIMIT = 400

    def __init__(self, size=(40, 30), fail=False):
        self.size = size
        self.fail = fail
        self.regions: list = []

    def __call__(self, region):
        self.regions.append(region)
        if self.fail:
            raise RuntimeError("grab failed")
        if len(self.regions) > self.LIMIT:
            raise RuntimeError("frame source exhausted (runaway capture loop)")
        from PIL import Image
        shade = (len(self.regions) * 7) % 256
        return Image.new("RGB", self.size, (shade, 40, 90)), 0, 0


@pytest.fixture(name="gif_env")
def _gif_env(monkeypatch):
    """假影格來源 ＋ 假時鐘。回 `(frames, clock)`。"""
    frames = _FakeFrames()
    clock = _FakeClock()
    monkeypatch.setattr(gui, "_logical_frame", frames)
    monkeypatch.setattr(gui, "time", clock)
    return frames, clock


def _gif_facts(path):
    """回 `(影格數, 每格毫秒, 尺寸)`——從真的存出來的檔案讀，不是從呼叫參數。"""
    from PIL import Image
    with Image.open(str(path)) as img:
        return img.n_frames, img.info.get("duration"), img.size


def test_capture_gif_writes_a_real_animation(tmp_path, gif_env):
    dest = tmp_path / "a.gif"
    count = gui.capture_gif(dest, seconds=3.0, fps=2.0)
    n_frames, duration, _size = _gif_facts(dest)
    assert count == n_frames == 6, "3 秒 × 2 fps 應該是 6 格"
    assert duration == 500, "每格毫秒數要跟影格率對得起來"


def test_capture_gif_clamps_the_duration_at_both_ends(tmp_path, gif_env):
    """上限擋的是「幾十 MB 送不出去」，下限擋的是「0 格」——後者會直接變成錯誤。"""
    long_dest = tmp_path / "long.gif"
    assert gui.capture_gif(long_dest, seconds=9_999.0, fps=1.0) == int(
        gui.GIF_MAX_SECONDS), "秒數沒有被夾到上限"
    short_dest = tmp_path / "short.gif"
    assert gui.capture_gif(short_dest, seconds=0.0, fps=1.0) >= 1, (
        "秒數夾到 0 會拍不到任何影格")


def test_capture_gif_clamps_the_frame_rate_at_both_ends(tmp_path, gif_env):
    """影格率的夾擠從**存出來的每格毫秒數**驗，不是從傳進去的參數。"""
    fast = tmp_path / "fast.gif"
    gui.capture_gif(fast, seconds=2.0, fps=999.0)
    assert _gif_facts(fast)[1] == int(1000 / gui.GIF_MAX_FPS)
    slow = tmp_path / "slow.gif"
    gui.capture_gif(slow, seconds=2.0, fps=0.01)
    assert _gif_facts(slow)[1] == 1000, "影格率下限是 1 fps"


def test_the_gif_clamps_are_safe_against_nan_because_the_constant_comes_first(
        tmp_path, gif_env):
    """這是 `_dorossi_clamp_usage_wait` 那個坑的**反例**，值得單獨釘住。

    `max(0.5, min(nan, 上限))` 回 0.5、`max(1.0, min(nan, 上限))` 回 1.0——因為
    CPython 的 `max(a, b)` 是「先取 a，再看 `b > a`」，而 nan 的比較恆為 False，
    所以常數在前就會贏。對調成 `min(max(值, 常數), 上限)` 之後：秒數變 nan →
    `time.monotonic() < nan` 恆為 False → 一格都拍不到；影格率變 nan →
    `rest > 0` 恆為 False → 迴圈完全不睡的空轉。兩種都不會有例外，只會安靜壞掉。
    """
    assert max(0.5, min(float("nan"), gui.GIF_MAX_SECONDS)) == 0.5
    assert max(1.0, min(float("nan"), gui.GIF_MAX_FPS)) == 1.0
    dest = tmp_path / "nan.gif"
    count = gui.capture_gif(dest, seconds=float("nan"), fps=float("nan"))
    assert count == 1, "nan 應該落到「最短一段、1 fps」，也就是剛好一格"
    assert _gif_facts(dest)[1] == 1000


def test_capture_gif_shrinks_an_oversized_desktop(tmp_path, monkeypatch):
    """整個桌面拍幾十張不縮圖是好幾十 MB，送不出去也沒人想看。"""
    frames = _FakeFrames(size=(gui.GIF_MAX_EDGE * 2, gui.GIF_MAX_EDGE))
    monkeypatch.setattr(gui, "_logical_frame", frames)
    monkeypatch.setattr(gui, "time", _FakeClock())
    dest = tmp_path / "big.gif"
    gui.capture_gif(dest, seconds=1.0, fps=1.0)
    assert max(_gif_facts(dest)[2]) == gui.GIF_MAX_EDGE


def test_capture_gif_leaves_a_small_frame_alone(tmp_path, gif_env):
    """反面：沒超過上限就不該縮——縮過的圖字會糊掉，而看清楚是這功能的重點。"""
    dest = tmp_path / "small.gif"
    gui.capture_gif(dest, seconds=1.0, fps=1.0)
    assert _gif_facts(dest)[2] == (40, 30)


def test_capture_gif_passes_the_region_through(tmp_path, gif_env):
    frames, _clock = gif_env
    region = [10, 20, 110, 220]
    gui.capture_gif(tmp_path / "r.gif", seconds=1.0, fps=1.0, region=region)
    assert frames.regions and all(r == region for r in frames.regions)


def test_capture_gif_reports_a_missing_imaging_library(tmp_path, monkeypatch):
    """`sys.modules["PIL"] = None` 讓 `from PIL import Image` 丟 ImportError，
    而且**不會**像 `sys.modules.pop` 那樣把真的套件放回來（見 test_suite_safety）。"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(GuiError) as caught:
        gui.capture_gif(tmp_path / "x.gif", seconds=1.0, fps=1.0)
    assert "not installed" in str(caught.value)


def test_capture_gif_reports_a_capture_failure_generically(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_logical_frame", _FakeFrames(fail=True))
    monkeypatch.setattr(gui, "time", _FakeClock())
    with pytest.raises(GuiError) as caught:
        gui.capture_gif(tmp_path / "x.gif", seconds=1.0, fps=1.0)
    assert "Continuous capture failed" in str(caught.value)
    assert "grab failed" not in str(caught.value), "原始例外文字外洩了"


def test_capture_gif_reports_a_save_failure_separately(tmp_path, gif_env):
    """存檔失敗跟拍不到影格是兩件事，訊息要分得開——一個是磁碟／路徑問題，
    另一個是擷取問題。"""
    missing = tmp_path / "no_such_dir" / "x.gif"
    with pytest.raises(GuiError) as caught:
        gui.capture_gif(missing, seconds=1.0, fps=1.0)
    assert "save" in str(caught.value)


def test_capture_gif_says_so_when_it_caught_nothing(tmp_path, monkeypatch):
    """時鐘一開始就過了截止時間 → 0 格。存一個空 GIF 會丟 IndexError，所以這一條
    必須自己有分支。"""
    class _AlreadyPast(_FakeClock):
        def monotonic(self):
            self.now += 100.0
            return self.now

    monkeypatch.setattr(gui, "_logical_frame", _FakeFrames())
    monkeypatch.setattr(gui, "time", _AlreadyPast())
    with pytest.raises(GuiError) as caught:
        gui.capture_gif(tmp_path / "x.gif", seconds=1.0, fps=1.0)
    assert "frames" in str(caught.value)
# --------------------------------------------------------------------------
# 別名表 vs 底層套件的真表
#
# 本模組有三張表在**描述另一個套件的內部現實**：`KEY_ALIASES` 的值、
# `MOUSE_BUTTONS` 的值、`_WRITE_CONTROL_KEYS_FALLBACK` 的值，全部是
# `je_auto_control` 那邊的鍵名。那個套件在本機是**可編輯安裝**、指向一份會被
# 實際修改的原始碼樹（見 `requirements.txt` 的說明），所以「它改名了」不是假想。
#
# 改名之後的症狀是零：`parse_key_name` 照樣跑、別名表照樣在，只是那一筆再也查不
# 到，使用者收到的是「不認得這個鍵名。」——那句話**不會**指向別名表。而且要有人
# 真的按下那顆鍵才會出現，按的人通常是無人值守的巨集。
#
# 原本的覆蓋為什麼接不住：`test_parse_hotkey_tokens_*` 只點名 36 個別名裡的
# **4** 個（`enter` / `esc` / `win` / `backspace`），其餘 32 個改壞了沒有任何東西
# 會紅；而所有走 `fake_ac` 的測試拿的是**假**的 `keyboard_keys_table`（四個鍵），
# 真表長什麼樣子整份測試從來沒問過。
#
# 2026-09-09 補這一道時三張表全部對得上（36 + 10 + 4 筆，真表 192 + 5 筆），
# 所以這不是在救火——是趁表還乾淨的時候把守門補上，那是最便宜的時機。
# --------------------------------------------------------------------------
@pytest.fixture(name="real_ac_tables")
def _real_ac_tables_fixture():
    """底層套件**真正**的鍵名表與滑鼠鍵表。拿不到就 skip。

    刻意直接 import，**不走** `gui.load_ac()`：那條路會把真模組寫進模組層的
    `_AC` / `_AC_TRIED` 快取，留給後面每一支用 `fake_ac` 的測試，變成測試互相
    汙染。這裡只要那兩張表，不需要經過本專案的載入層。
    """
    if not sys.platform.startswith("win"):
        pytest.skip("鍵名表是 Win32 虛擬鍵；其他平台的後端是另一張表")
    wrapper = pytest.importorskip(
        "je_auto_control.wrapper.platform_wrapper",
        reason="je-auto-control 沒安裝（requirements.txt 有列，正常不該發生）")
    keys = getattr(wrapper, "keyboard_keys_table", None)
    mouse = getattr(wrapper, "mouse_keys_table", None)
    # 正面對照組。空表或殘表會讓底下每一句「都在表裡」**空轉通過**，而那跟
    # 「全部都對」在輸出上長得一模一樣。
    assert isinstance(keys, dict) and len(keys) >= 100, (
        "後端鍵名表只有 %s 筆，抓錯東西了——底下那些斷言的綠色會是假的"
        % (len(keys) if isinstance(keys, dict) else type(keys).__name__))
    assert isinstance(mouse, dict) and len(mouse) >= 3, (
        "後端滑鼠鍵表只有 %s 筆"
        % (len(mouse) if isinstance(mouse, dict) else type(mouse).__name__))
    return keys, mouse


def _targets_missing_from(table, aliases):
    """`aliases` 的值裡、`table` 查不到的那些（去重後排序）。

    抽成純函式是為了讓它能有**自己的**正面對照組：表乾淨的時候，把下面幾支主
    測試的斷言整個拿掉本來就不會有人紅，所以「這個比對還看得見東西嗎」必須另外
    拿合成資料問一次，見 `test_the_alias_reconciler_would_see_a_rename`。
    """
    return sorted({value for value in aliases.values() if value not in table})


def test_every_key_alias_points_at_a_real_backend_key(real_ac_tables):
    """鍵名別名的**目標**都要是送得出去的鍵：後端表裡有，或在 `_EXTRA_KEY_CODES`。

    後者（`plus` → `oem_plus` 那四個，2026-09-21）由 `_library_key` 換成整數
    再交給函式庫，不依賴後端表的名字，所以不會因為後端改名而漂掉。
    """
    keys, _ = real_ac_tables
    assert len(gui.KEY_ALIASES) >= 30, "別名表空了，這一筆會永遠通過"
    sendable = {**keys, **gui._EXTRA_KEY_CODES}
    missing = _targets_missing_from(sendable, gui.KEY_ALIASES)
    assert not missing, (
        "`KEY_ALIASES` 這些目標鍵名在後端表裡不存在：%s。後端改名之後別名就變成"
        "查不到的字串，使用者只會收到「不認得這個鍵名。」，看不出問題在別名表。"
        % missing)


def test_no_key_alias_shadows_a_real_key_name(real_ac_tables):
    """別名的**鍵**不可以自己就是一個合法鍵名，否則會**遮蔽**那顆真鍵。

    `parse_key_name` 是先查別名再驗表（`KEY_ALIASES.get(key, key)`），所以後端哪
    天新增一顆叫 `enter` 的鍵，這裡的 `enter -> return` 會把它悄悄改道到別的鍵。
    使用者按到的不是他打的那顆，而且完全沒有訊息——比查不到還難查。

    **判準是「改道到別的鍵」，不是「名字撞到」。** 後端可以同時收兩個名字指向**同一
    個**虛擬鍵碼（實測：`ctrl` 與 `control` 都是 17，`ctrl` 是後端後來才補上的）。
    那種情況下別名沒有改道任何東西，使用者按到的就是他打的那顆；把它算成違規，等於
    逼人刪掉一個「後端哪天又拿掉 `ctrl`」就會需要的別名，換來的安全是零。所以比的是
    **鍵碼**：兩邊碼不同才是遮蔽。
    """
    keys, _ = real_ac_tables
    shadowed = sorted(name for name, target in gui.KEY_ALIASES.items()
                      if name in keys and keys.get(target) != keys[name])
    assert not shadowed, (
        "這些別名的名字本身就是後端的合法鍵名，而且**指向不同的鍵碼**：%s。"
        "`parse_key_name` 先查別名，所以那顆真鍵會被**靜默改道**。"
        "請把該別名刪掉或改名。" % shadowed)


def test_the_shadowing_check_still_catches_a_real_redirect():
    """對照組：真實資料今天沒有「改道」的別名，所以上面那句斷言恆真——刪掉也全綠。

    兩種輸入各問一次：碼相同（只是同義字，放行）與碼不同（真的改道，要抓到）。
    """
    keys = {"enter": 13, "return": 13, "ctrl": 17, "control": 17, "f1": 112}

    def shadowed(aliases):
        return sorted(name for name, target in aliases.items()
                      if name in keys and keys.get(target) != keys[name])

    assert shadowed({"ctrl": "control", "enter": "return"}) == []
    assert shadowed({"enter": "f1"}) == ["enter"], "真的改道卻沒被抓到"
    assert shadowed({"nosuchkey": "f1"}) == [], "名字沒撞到就不歸這支管"


def test_every_mouse_button_points_at_a_real_backend_button(real_ac_tables):
    """滑鼠鍵別名（含側鍵 `x1` / `x2`）的目標也要在後端表裡。"""
    _, mouse = real_ac_tables
    assert len(gui.MOUSE_BUTTONS) >= 6, "滑鼠別名表空了，這一筆會永遠通過"
    missing = _targets_missing_from(mouse, gui.MOUSE_BUTTONS)
    assert not missing, (
        "`MOUSE_BUTTONS` 這些目標在後端滑鼠鍵表裡不存在：%s" % missing)


def test_the_write_control_key_fallback_names_real_keys(real_ac_tables):
    """`write()` 的控制字元備援表也是後端鍵名，一樣會漂。

    這一份**有**優雅降級（`_write_control_keys` 讀得到後端的真表就用真表），
    所以漂掉的代價比上面兩張小；但降級只在後端**還有**那個常數時成立，常數被拿
    掉之後就只剩這份備援，而那正是最需要它是對的時候。
    """
    keys, _ = real_ac_tables
    fallback = gui._WRITE_CONTROL_KEYS_FALLBACK
    assert len(fallback) >= 3, "備援表空了，這一筆會永遠通過"
    missing = _targets_missing_from(keys, fallback)
    assert not missing, (
        "`_WRITE_CONTROL_KEYS_FALLBACK` 這些鍵名後端不認得：%s" % missing)


def test_every_alias_survives_parse_key_name_against_the_real_table(
        real_ac_tables, monkeypatch):
    """端到端：每一個別名都要真的走得過 `parse_key_name`，用**真**表驗。

    上面那幾支比的是常數，這一支比的是**使用者按下去會發生什麼**——中間還隔著
    `HOTKEY_TOKEN_RE`、小寫正規化，以及「後端載不進來就只做字面檢查」那條提前
    return。走完整條路才能保證 `/input key press ctrl` 這種文件裡的每個範例真的
    可用。
    """
    keys, _ = real_ac_tables

    class _TableOnly:
        keyboard_keys_table = keys

    monkeypatch.setattr(gui, "_AC", _TableOnly())
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    for alias, target in sorted(gui.KEY_ALIASES.items()):
        assert gui.parse_key_name(alias) == target, alias
        assert gui.parse_key_name(alias.upper()) == target, alias
    # 反面：真表在的時候，不存在的鍵名一定要被擋下來（否則上面全綠也不代表
    # 這條路徑真的有在驗）。
    with pytest.raises(GuiError):
        gui.parse_key_name("definitely_not_a_key")


def test_the_alias_reconciler_would_see_a_rename():
    """比對器自己的正面對照組：拿合成資料確認它真的看得見漂掉的目標。

    需要這一支的理由跟 `test_the_stale_detector_would_see_a_rename`
    （`test_bot_helpers.py`）一樣：表乾淨時，上面那幾支主測試**把斷言整個拿掉也
    不會紅**。真正在做事的是 `_targets_missing_from`，所以牙齒要長在這裡。
    """
    table = {"control": 17, "return": 13}
    assert _targets_missing_from(table, {"ctrl": "control"}) == []
    # 後端把 `control` 改名成 `ctrl_key` → 別名的目標查不到了。
    assert _targets_missing_from(
        {"ctrl_key": 17}, {"ctrl": "control"}) == ["control"]
    # 去重：兩個別名指向同一個漂掉的目標只報一次。
    assert _targets_missing_from(
        {}, {"win": "lwin", "cmd": "lwin"}) == ["lwin"]
    # 方向只有一邊：後端有、別名沒指到的鍵**不是**漂掉（絕大多數鍵本來就沒有
    # 別名）。方向寫反的話這一筆會紅。
    assert _targets_missing_from(table, {"enter": "return"}) == []


# --------------------------------------------------------------------------
# PowerShell 的編碼：前綴（兩端）＋ 圍堵旗標
#
# 判準是「同一次呼叫的輸出流裡可以有兩種編碼」，所以守門必須有一個**混合**語料，
# 而不是只驗「中文出得來」。只驗中文的話，把讀的那端改成 `encoding="oem"` 也會
# 全綠——而那會反過來把原生命令（git 之類）吐的 UTF-8 整個弄壞。
#
# 三條性質各自獨立，缺一不可，所以分三支：
#   * 前綴要有**兩句**（只設輸出端的話 `job_send` 餵中文仍然壞，且沒有錯誤訊息）；
#   * **兩個** spawn 點都要帶 `CREATE_NO_WINDOW`（漏掉一個就從那一個開始污染）；
#   * 旗標本身要用平台條件包起來（拿掉之後在 Windows 上**行為完全不變**，只有
#     非 Windows 會在 import 期就 `AttributeError`——這台機器上沒有任何行為測試
#     看得到，只能靠靜態檢查）。
# --------------------------------------------------------------------------
def test_the_shell_prelude_sets_both_console_encodings():
    """前綴要兩句都在，而且使用者的指令原封不動留在後面。"""
    if not sys.platform.startswith("win"):
        pytest.skip("PowerShell 前綴只在 Windows 上有意義")
    command = "Write-Output 'x'"
    for argv in (gui.shell_argv(command),
                 gui.shell_argv(command, interactive=True)):
        tail = argv[-1]
        assert "[Console]::OutputEncoding=[Text.Encoding]::UTF8" in tail, (
            "輸出端沒有被設成 UTF-8：PowerShell **自己**的輸出會走主控台代碼頁"
            f"（本機 cp950），而我們是照 utf-8 讀的。argv 尾端：{tail!r}")
        assert "[Console]::InputEncoding=[Text.Encoding]::UTF8" in tail, (
            "輸入端沒有被設成 UTF-8。`job_send` 用 UTF-8 寫 stdin、PowerShell "
            "用主控台代碼頁解——實測 `測試輸入`（4 個字元）進去變成 6 個字元，"
            "而**回顯會把這個傷害藏起來**（mojibake 再編回同一個代碼頁就是原本"
            f"那串位元組）。argv 尾端：{tail!r}")
        assert tail.endswith(command), (
            f"使用者的指令被前綴動到了：{tail!r}")


def test_both_shell_spawn_points_get_their_own_console(monkeypatch):
    """`run_shell` 與 `job_start` **都**要帶圍堵旗標。

    前綴那兩個 setter 底下是 `SetConsoleOutputCP` / `SetConsoleCP`，打的是**共用
    的**主控台而且不會還原。實測（隔離在子行程自己的主控台裡）：沒有旗標時
    代碼頁 950 → 65001，而且之後**沒有加前綴**的呼叫也跟著吐 UTF-8。放進 bot 就是
    每跑一次 `/host sh run` 污染一次，兩處刻意用 `encoding="oem"` 的 `schtasks`
    查詢會反過來壞掉——症狀出現在別的功能上，不會指回這裡。

    兩個站點分開驗：漏掉其中一個的話，污染就從那一個開始，而另一個是乾淨的。
    """
    if not sys.platform.startswith("win"):
        pytest.skip("`CREATE_NO_WINDOW` 只存在於 Windows")
    seen: list[dict] = []

    import io

    class _FakeProc:
        returncode = 0
        pid = -1
        stdout = io.StringIO("")
        stdin = None

        def communicate(self, timeout=None):
            return "", ""

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def _fake_popen(argv, **kwargs):
        seen.append(kwargs)
        return _FakeProc()

    monkeypatch.setattr(gui.subprocess, "Popen", _fake_popen)
    gui.run_shell("echo hi", timeout=5)
    gui.job_start("echo hi")
    assert len(seen) == 2, f"預期兩個 spawn 點各一次，實際 {len(seen)}"
    for label, kwargs in zip(("run_shell", "job_start"), seen):
        assert kwargs.get("creationflags") == gui.subprocess.CREATE_NO_WINDOW, (
            f"`{label}` 的 spawn 沒有帶 `CREATE_NO_WINDOW`，實際是 "
            f"{kwargs.get('creationflags')!r}。子行程會沿用 bot 的主控台，於是"
            "`shell_argv` 的前綴把 bot 那一個的代碼頁改掉。")


def test_the_no_window_flag_is_guarded_by_a_platform_check():
    """旗標常數要用平台條件包起來——這一條**只有**靜態檢查看得到。

    `subprocess.CREATE_NO_WINDOW` 只存在於 Windows。拿掉條件之後，在這台機器上
    行為完全不變（屬性本來就在），所有行為測試照樣全綠；壞的是別的平台，而且是
    在 import 期就 `AttributeError`，整個 bot 起不來。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gui))
    value = None
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign)
                   else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_SHELL_CREATIONFLAGS":
                value = node.value
    assert value is not None, (
        "找不到模組層的 `_SHELL_CREATIONFLAGS`——兩個 spawn 點都指著它。")
    assert isinstance(value, ast.IfExp), (
        "`_SHELL_CREATIONFLAGS` 不再是條件式了："
        f"{ast.unparse(value)}。`subprocess.CREATE_NO_WINDOW` 只存在於 Windows。")
    test_src = ast.unparse(value.test)
    assert ("os.name" in test_src and "'nt'" in test_src) or (
        "sys.platform" in test_src and "win" in test_src), (
        f"條件不是平台判斷：{test_src}")


def test_a_mixed_encoding_command_survives_the_round_trip():
    """**混合**語料：同一次呼叫裡 PowerShell 自己的中文 ＋ 原生命令的 UTF-8。

    這一支是這批守門的核心。只驗「中文出得來」的話，把讀的那端改成
    `encoding="oem"` 也會全綠——而那會反過來把原生命令吐的 UTF-8 整個弄壞，
    也就是原本那個缺陷換一邊犯。原生命令這一半用的是**另一個 Python 直譯器**
    （明確餵它 `PYTHONIOENCODING`），所以它真的是「原樣穿透的 UTF-8 位元組」，
    不必依賴 repo 的 git 歷史。

    實測（2026-09-12，乾淨環境）：修前 11 個 U+FFFD（PowerShell 那一半全毀、
    原生那一半完好），修後 0 個。
    """
    if not sys.platform.startswith("win"):
        pytest.skip("這個缺陷是 Windows 主控台代碼頁造成的")
    own = "佇列已清空，行程結束"
    native = "原生命令的輸出"
    exe = sys.executable.replace("'", "''")
    command = (
        f"Write-Output '{own}'; "
        "$env:PYTHONIOENCODING='utf-8'; "
        f"& '{exe}' -c \"print('{native}')\"")
    result = gui.run_shell(command, timeout=90)
    assert result["rc"] == 0, result
    assert "�" not in result["output"], (
        "輸出裡有替換字元，表示有一半的編碼被猜錯了："
        f"{result['output']!r}")
    assert own in result["output"], (
        f"PowerShell 自己的中文沒出來：{result['output']!r}")
    assert native in result["output"], (
        "原生命令原樣穿透的 UTF-8 沒出來——只修好其中一半的話這一行會紅："
        f"{result['output']!r}")


def test_an_interactive_job_receives_chinese_input_intact():
    """輸入端的往返。**量字元數，不要量回顯。**

    回顯會把傷害藏起來：`Read-Host` 拿到的 mojibake 再用同一個代碼頁編碼寫回
    stdout，位元組跟原本的 UTF-8 一模一樣，照 utf-8 解就「看起來完全正確」。
    `測試輸入` 是 4 個字元、UTF-8 12 個位元組；被當 cp950 解會變成 6 個。
    實測：修前 LEN=6，修後 LEN=4。

    互動模式**不能**加 `-NonInteractive`（那會讓 `Read-Host` 直接報錯），現行
    `shell_argv` 已經是這樣分的。
    """
    if not sys.platform.startswith("win"):
        pytest.skip("這個缺陷是 Windows 主控台代碼頁造成的")
    job_id = gui.job_start('$x = Read-Host; Write-Output ("LEN=" + $x.Length)',
                           interactive=True)
    try:
        gui.job_send(job_id, "測試輸入")
        deadline = time.time() + 60
        lines: list[str] = []
        while time.time() < deadline:
            info = gui.job_log(job_id, 50)
            lines = str(info.get("text") or "").splitlines()
            if any(l.startswith("LEN=") for l in lines):
                break
            time.sleep(0.3)
        got = [l for l in lines if l.startswith("LEN=")]
        assert got == ["LEN=4"], (
            "PowerShell 收到的不是 4 個字元——我們用 UTF-8 寫 stdin，它用主控台"
            f"代碼頁解。實際收到：{got!r}（整份輸出 {lines!r}）")
    finally:
        try:
            gui.job_stop(job_id)
        except Exception:  # pylint: disable=broad-except
            pass


# --------------------------------------------------------------------------
# 性質：存檔驗證與重播對同一個參數的解讀必須一致（衍生守門，2026-09-21）
# --------------------------------------------------------------------------
#
# `parse_macro_steps` 的承諾是「驗證在存檔時就做一次，壞掉的巨集不會等到重播到一半
# 才炸」。2026-09-21 用差分探針量到六個動詞違反它：`if_pixel` / `if_no_pixel` 的第四
# 個參數驗證端當秒數、執行端當整數容差；`wait_ui` / `wait_gone_text` /
# `wait_gone_window` / `click_text` 的前導秒數驗證端根本沒看、執行端卻會解析。
# 上面那幾支逐案測試釘住的是**那六個**；這一支釘的是**形狀**——`MACRO_VERBS` 裡每一個
# 動詞（包括以後新增的）都自動納入，驗證端放行的每一步，拿到執行端都不得因為參數
# 解讀而失敗。碰桌面的函式全部換成回傳固定值的替身，所以執行端丟出的任何例外都只
# 可能來自參數解讀。


# 在 `run_macro_program` 裡就處理掉、不會走到 `run_macro_step` 的控制流動詞。
_MACRO_CONTROL_FLOW = frozenset({"repeat", "else", "end", "stop", "call"})

# 語料：前三個位置用完整的池，第四個以後只用數字——第四個位置只有 `drag` 的座標、
# 三個 `…pixel` 動詞的容差／秒數與 `win` 的標題會用到，數字已經涵蓋會出事的那幾種。
_MACRO_PARITY_POOL = ("0", "-1", "2.5", "150", "256", "nan", "#ffffff", "存檔",
                      "left", "min", "set", "ctrl+s")
_MACRO_PARITY_TAIL_POOL = ("0", "-1", "2.5", "150", "256", "nan")

# 碰桌面的函式 → 替身回傳值（形狀要對，否則執行端的解包會失敗而變成假警報）。
_MACRO_DESKTOP_STUBS = {
    "mouse_click": None, "mouse_move": None, "mouse_drag": None,
    "mouse_scroll": None, "type_text": None, "paste_text": None,
    "press_hotkey": None, "key_down": "k", "key_up": "k",
    "release_all_inputs": [], "release_all_inputs_report": ([], []),
    "window_focus": (1, "t", 1),
    "window_close": (1, "t", 1), "window_show": (1, "t", 1),
    "wait_window": None, "wait_text": (1, 2), "click_text": (1, 2),
    "set_clipboard": None, "ui_click": (1, 2),
    "ui_wait": {"x": 1, "y": 2}, "wait_text_gone": None,
    "wait_window_gone": None, "wait_pixel": (1, 2, 3),
    "_sleep_abortable": None, "pixel_color": (250, 250, 250),
    "find_text": [], "match_windows": [], "ui_find": None,
}


class _DesktopTripwire:
    """任何一次碰到底層函式庫都記下來。

    刻意**不丟例外**：本模組好幾個呼叫點包在寬的 `except` 裡，一個會爆的替身在
    那裡什麼都證明不了。記錄留在這裡，事後斷言它是空的。

    唯一放行的是**資料**：存檔驗證會拿後端的鍵名表檢查 `hotkey` / `keydown` 的鍵名，
    那是查表不是動作。給它一張夠語料用的小表，而不是讓它落進 `__getattr__`——
    那樣驗證端會拿到一個函式當表，改走另一條路，量到的就不是真的驗證了。
    """

    keyboard_keys_table = {"control": 17, "shift": 16, "s": 83, "left": 37}

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        return lambda *args, **kwargs: None


def _macro_corpus(verb: str, low: int, high: int | None, pool, tail_pool):
    """某個動詞的所有候選步驟（參數個數從 `low` 到 `min(high, 4)`）。"""
    top = min(high if high is not None else low + 2, 4)
    for count in range(low, top + 1):
        pools = [pool if index < 3 else tail_pool for index in range(count)]
        for combo in itertools.product(*pools):
            yield " ".join((verb, *combo))


def _macro_parity_problems(verbs, validate, execute, pool, tail_pool):
    """回 `(每個動詞放行了幾步, 問題清單)`。

    問題＝驗證端放行、執行端卻丟例外的步驟；每個動詞只留第一個例子與總數，否則
    一個動詞壞掉就會印出上千行。
    """
    accepted: dict[str, int] = {}
    problems: list[str] = []
    for verb, (low, high) in sorted(verbs.items()):
        accepted[verb] = 0
        first: str | None = None
        failures = 0
        for step in _macro_corpus(verb, low, high, pool, tail_pool):
            try:
                validate(step)
            except GuiError:
                continue
            accepted[verb] += 1
            try:
                execute(step)
            except Exception as error:  # pylint: disable=broad-except
                failures += 1
                if first is None:
                    first = f"{step!r} → {type(error).__name__}: {error}"
        if failures:
            problems.append(f"`{verb}`：{failures} 步驗證放行但執行失敗，例如 {first}")
    return accepted, problems


def _execute_macro_step(step: str) -> None:
    verb, args = gui.validate_macro_step(step)
    if verb in gui.MACRO_CONDITIONS:
        gui.eval_macro_condition(verb, args)
    else:
        gui.run_macro_step(step)


def test_the_macro_parity_check_can_actually_see_a_disagreement():
    """合成語料：驗證端與執行端對同一個參數的解讀不同時，必須被報出來。

    真實樹是乾淨的，所以「報出問題」那幾行在真實資料上從來不會執行——刪掉它們
    整個守門照樣綠。這裡給它一個確定會分歧的動詞、一個確定一致的動詞。
    """
    def validate(step: str) -> None:
        verb, *args = step.split()
        if verb == "lenient" and args and args[0] not in ("0", "2.5"):
            raise GuiError("拒收。")
        if verb == "strict" and args and args[0] != "0":
            raise GuiError("拒收。")

    def execute(step: str) -> None:
        verb, *args = step.split()
        int(args[0])   # 兩個動詞的執行端都只吃整數

    verbs = {"lenient": (1, 1), "strict": (1, 1)}
    accepted, problems = _macro_parity_problems(
        verbs, validate, execute, ("0", "2.5", "x"), ("0",))
    assert accepted == {"lenient": 2, "strict": 1}, accepted
    assert len(problems) == 1, problems
    assert "`lenient`" in problems[0] and "'lenient 2.5'" in problems[0], problems
    assert "1 步" in problems[0], problems


@pytest.fixture(name="stubbed_desktop")
def _stubbed_desktop_fixture(monkeypatch):
    """碰桌面的高階函式全部換成替身，底層函式庫換成只記錄的絆線；回絆線。"""
    tripwire = _DesktopTripwire()
    monkeypatch.setattr(gui, "load_ac", lambda: tripwire)
    for name, value in _MACRO_DESKTOP_STUBS.items():
        monkeypatch.setattr(gui, name, lambda *a, _v=value, **k: _v)
    return tripwire


def test_the_control_flow_verbs_the_parity_check_skips_are_really_control_flow(
        stubbed_desktop):
    """跳過清單只能放 `run_macro_step` 真的不處理的動詞。

    多放一個動作動詞進來，那個動詞就安靜地退出底下的守門——而「它在
    `MACRO_VERBS` 裡」這種檢查擋不住，因為它確實在（變異測試量到的：把 `wait_ui`
    加進跳過清單，只做存在性檢查時整組照樣綠）。所以直接問執行端：它對這幾個
    動詞的回答必須是「不認得」。
    """
    assert _MACRO_CONTROL_FLOW <= set(gui.MACRO_VERBS), (
        _MACRO_CONTROL_FLOW - set(gui.MACRO_VERBS))
    assert not _MACRO_CONTROL_FLOW & set(gui.MACRO_CONDITIONS)
    for verb in sorted(_MACRO_CONTROL_FLOW):
        low, high = gui.MACRO_VERBS[verb]
        steps = [step for step in _macro_corpus(
            verb, low, high, _MACRO_PARITY_POOL, _MACRO_PARITY_TAIL_POOL)
            if _validates(step)]
        assert steps, f"語料生不出 `{verb}` 的合法步驟"
        with pytest.raises(GuiError, match="Unknown action"):
            gui.run_macro_step(steps[0])
    assert not stubbed_desktop.touched, stubbed_desktop.touched


def _validates(step: str) -> bool:
    try:
        gui.validate_macro_step(step)
    except GuiError:
        return False
    return True


def test_every_step_the_validator_accepts_runs_without_a_parse_failure(
        stubbed_desktop):
    tripwire = stubbed_desktop
    verbs = {verb: arity for verb, arity in gui.MACRO_VERBS.items()
             if verb not in _MACRO_CONTROL_FLOW}
    accepted, problems = _macro_parity_problems(
        verbs, gui.validate_macro_step, _execute_macro_step,
        _MACRO_PARITY_POOL, _MACRO_PARITY_TAIL_POOL)
    # 下限：語料對每個動詞都至少要生出一步合法的，否則那個動詞等於沒被檢查。
    silent = sorted(verb for verb, count in accepted.items() if count == 0)
    assert not silent, (
        f"語料生不出這些動詞的任何一步合法步驟，等於沒檢查：{silent}；"
        "把它需要的參數形狀加進 `_MACRO_PARITY_POOL`。")
    assert len(accepted) >= 25, accepted
    assert not problems, (
        "存檔驗證放行、重播時卻因為參數解讀失敗的步驟（驗證端與執行端要呼叫同一支"
        "解析函式）：\n" + "\n".join(problems))
    assert not tripwire.touched, (
        f"有動詞碰到了真的桌面函式庫：{sorted(set(tripwire.touched))}；"
        "把它呼叫的高階函式加進 `_MACRO_DESKTOP_STUBS`。")


# --------------------------------------------------------------------------
# 事前檢查：第一個動作之前把代入後的整個程式驗完（2026-09-21）
#
# 直譯器是一邊跑一邊代入 `$N`、一邊驗證的，所以 `['click 10 10', 'type $1']`
# 沒給參數時，點選已經真的點下去了才發現第二行壞了；`call` 到不存在的巨集、
# 遞迴超過深度上限也一樣。這一節的每一支都用 `macro_spy` 記錄底層動作，斷言
# 的是**一個都沒送出**——而每一組都配一支「合法的照樣整串跑完」，否則「一律
# 拒絕」也會全綠。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("steps, args, line", [
    (["click 10 10", "type $1"], [], 2),                 # 少一個參數
    (["click 10 10", "wait_ui $1 存檔"], ["150"], 2),    # 參數代進去超過上限
    (["click 10 10", "if_text $1", "type x", "end"], [], 2),
    # 執行時走不到的步驟一樣要驗：走到才會壞的步驟就是壞的
    (["click 10 10", "repeat 0", "type $1", "end"], [], 3),
    (["click 10 10", "stop", "type $2"], ["a"], 3),
])
def test_a_program_broken_after_substitution_does_nothing_at_all(
        macro_spy, steps, args, line):
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(steps, args=args, should_abort=_never_abort)
    assert macro_spy.calls == [], (
        f"壞掉的程式已經送出 {macro_spy.names}——第 {line} 行的問題必須在第一個"
        "動作之前就被擋下。")
    assert f"Line {line}" in str(excinfo.value), str(excinfo.value)


def test_a_valid_substituted_program_still_runs_every_step(macro_spy):
    """反方向：事前檢查放行的程式照樣從頭跑到尾，參數也真的代進去了。"""
    done = gui.run_macro_program(
        ["click 10 10", "type $1", "wait_ui $2 存檔"], args=["hi", "12"],
        should_abort=_never_abort)
    assert macro_spy.names == ["mouse_click", "type_text", "ui_wait"]
    assert macro_spy.calls[1] == ("type_text", ("hi",), {})
    assert macro_spy.calls[2][1][:2] == ("存檔", 12.0)
    assert len(done) == 3


def test_the_executor_still_validates_each_step_lazily(macro_spy, monkeypatch):
    """事前檢查不是唯一一道：長巨集跑到一半，被呼叫的巨集檔還是可能被改掉。

    把事前檢查換成什麼都不做，逐步驗證仍然要在壞掉的那一步擋下來——拿掉迴圈
    裡那一道的話，這支會紅。
    """
    monkeypatch.setattr(gui, "check_macro_program", lambda *a, **k: None)
    with pytest.raises(GuiError):
        gui.run_macro_program(["click 10 10", "type $1"],
                              should_abort=_never_abort)
    assert macro_spy.names == ["mouse_click"]

    # 動作步驟另有 `run_macro_step` 自己再驗一次，所以上面那組分不出迴圈裡那一道
    # 在不在。控制流步驟不經過 `run_macro_step`：代入後缺參數的 `if_text` 必須在
    # 迴圈裡就被擋下，不能帶著空字串去跑辨識。
    asked: list = []
    monkeypatch.setattr(gui, "find_text", lambda *a, **k: asked.append(a) or [])
    macro_spy.reset()
    with pytest.raises(GuiError):
        gui.run_macro_program(["if_text $1", "click 1 1", "end"],
                              should_abort=_never_abort)
    assert asked == [] and macro_spy.calls == [], (asked, macro_spy.calls)


def test_a_missing_callee_is_caught_before_the_first_action(macro_spy, macro_dir):
    del macro_dir
    gui.save_macro("outer", ["click 1 1", "call ghost"])
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(gui.load_macro("outer")["steps"],
                              should_abort=_never_abort)
    assert macro_spy.calls == []
    message = str(excinfo.value)
    assert "Line 2" in message and "`ghost`" in message, message
    assert "No macro with that name was found" in message, message


def test_a_missing_callee_in_a_branch_that_never_runs_is_still_caught(
        macro_spy, macro_dir, monkeypatch):
    del macro_dir
    monkeypatch.setattr(gui, "match_windows", lambda needle: [])
    with pytest.raises(GuiError):
        gui.run_macro_program(
            ["click 1 1", "if_window 不會有", "call ghost", "end"],
            should_abort=_never_abort)
    assert macro_spy.calls == []


def _save_chain(prefix: str, length: int, leaf: str = "type x") -> str:
    """存一條 `prefix0 → prefix1 → … → prefix{length-1}` 的呼叫鏈，回頭一個的名字。"""
    for index in range(length):
        step = (f"call {prefix}{index + 1}" if index + 1 < length else leaf)
        gui.save_macro(f"{prefix}{index}", [step])
    return f"{prefix}0"


def test_a_call_chain_at_the_depth_cap_runs(macro_spy, macro_dir):
    """深度剛好到上限的呼叫鏈照樣跑——過度收緊跟漏接一樣糟。"""
    del macro_dir
    head = _save_chain("ok", gui.MACRO_MAX_CALL_DEPTH)     # 最外層 0 ＋ 三層 call
    gui.run_macro_program(["click 1 1", f"call {head}"],
                          should_abort=_never_abort)
    assert macro_spy.names == ["mouse_click", "type_text"]


def test_a_call_chain_past_the_depth_cap_does_nothing(macro_spy, macro_dir):
    del macro_dir
    head = _save_chain("deep", gui.MACRO_MAX_CALL_DEPTH + 1)
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(["click 1 1", f"call {head}"],
                              should_abort=_never_abort)
    assert macro_spy.calls == []
    # 跟執行端丟的是同一句話
    expected = f"The macro call depth exceeds the limit ({gui.MACRO_MAX_CALL_DEPTH} levels)."
    assert str(excinfo.value).endswith(expected), str(excinfo.value)


def test_a_call_chain_that_deepens_mid_run_is_stopped_by_the_executor(macro_spy, macro_dir):
    """預先驗證通過之後，被呼叫的巨集檔在執行途中被改深——那時只剩執行端那道深度檢查。

    `check_macro_program` 在第一個動作之前就把超過上限的鏈擋掉（上面那支），所以
    `run_macro_program` 開頭的 `depth > MACRO_MAX_CALL_DEPTH` 在整個套件裡從來沒有成立過
    （2026-09-22 分支覆蓋率）。它存在的理由正是「長巨集跑的途中，被呼叫的巨集檔被改掉」：
    這裡在第一個動作做完時，把最深那一層改成再 `call` 一層。"""
    del macro_dir
    head = _save_chain("grow", gui.MACRO_MAX_CALL_DEPTH)
    deepest = f"grow{gui.MACRO_MAX_CALL_DEPTH - 1}"

    def _deepen(_index, _raw, _detail):
        if not gui.load_macro(deepest)["steps"][0].startswith("call"):
            gui.save_macro("extra", ["type y"])
            gui.save_macro(deepest, ["call extra"])

    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(["click 1 1", f"call {head}"], on_step=_deepen,
                              should_abort=_never_abort)
    expected = f"The macro call depth exceeds the limit ({gui.MACRO_MAX_CALL_DEPTH} levels)."
    assert str(excinfo.value).endswith(expected), str(excinfo.value)
    assert macro_spy.names == ["mouse_click"], "多出來的那一層一個動作都不該做"


def test_a_conditional_self_call_is_caught_before_the_first_action(
        macro_spy, macro_dir):
    """`if_text 錯誤` 裡 `call` 自己：整個展開一定超過深度上限，所以事前就擋。"""
    del macro_dir
    gui.save_macro("retry", ["click 1 1", "if_text 錯誤", "call retry", "end"])
    with pytest.raises(GuiError) as excinfo:
        gui.run_macro_program(gui.load_macro("retry")["steps"],
                              should_abort=_never_abort)
    assert macro_spy.calls == []
    assert "call depth exceeds the limit" in str(excinfo.value)


def test_the_memo_does_not_hide_a_depth_overflow_on_a_deeper_path(macro_dir):
    """同一個 `(巨集, 參數)` 在淺處驗得過，在深處不一定。

    `k` 的呼叫樹高 1：從深度 1 呼叫沒事，從深度 3 呼叫就超過上限。記憶化若只記
    「驗過了」，第二次遇到 `k` 會直接放行——而執行端走到那條路時一定會失敗。
    """
    del macro_dir
    gui.save_macro("leaf", ["type x"])
    gui.save_macro("k", ["call leaf"])
    gui.save_macro("m", ["call n"])
    gui.save_macro("n", ["call k"])
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(["call k", "call m"])
    assert "call depth exceeds the limit" in str(excinfo.value)
    # 前提：單走淺的那條是合法的，失敗真的來自深的那條
    gui.check_macro_program(["call k"])


def test_the_check_memoises_repeated_callees(macro_dir, monkeypatch):
    """四層、每層 20 行、參數原樣往下傳：不記憶化要驗十六萬次，記憶化後幾十次。"""
    del macro_dir
    gui.save_macro("d", ["type $1"] * 20)
    gui.save_macro("c", ["call d $1"] * 20)
    gui.save_macro("b", ["call c $1"] * 20)
    counts = {"validate": 0, "load": 0}
    real_validate, real_load = gui.validate_macro_step, gui.load_macro

    def _validate(step):
        counts["validate"] += 1
        return real_validate(step)

    def _load(name):
        counts["load"] += 1
        return real_load(name)

    monkeypatch.setattr(gui, "validate_macro_step", _validate)
    monkeypatch.setattr(gui, "load_macro", _load)
    gui.check_macro_program(["call b $1"] * 20, ["x"])
    # 20 × 4 層代入後的步驟 ＋ 三次讀檔各重驗 20 行 ＝ 140
    assert counts["load"] == 3, counts
    assert counts["validate"] <= 200, counts

    # 同一個被呼叫者、不同的參數：各驗一次，但檔案只讀一次
    counts.update(validate=0, load=0)
    gui.check_macro_program([f"call d {i}" for i in range(20)])
    assert counts["load"] == 1, counts
    assert counts["validate"] == 20 + 20 + 20 * 20, counts


def test_a_fan_out_with_distinct_arguments_hits_the_check_budget(
        macro_dir, monkeypatch):
    """記憶化不是上界：每一層用字面參數分出不同的鍵，工作量照樣是乘出來的。

    這張圖要驗 20 ＋ 400 ＋ 8000 次；上限調到 500 必須在半路停下，而不是跑完。
    """
    del macro_dir
    gui.save_macro("cc", ["type $1 $2"] * 20)
    gui.save_macro("bb", [f"call cc $1 {j}" for j in range(20)])
    top = [f"call bb {i}" for i in range(20)]
    gui.check_macro_program(top)                  # 前提：預設上限下是合法的
    monkeypatch.setattr(gui, "MACRO_MAX_CHECKED", 500)
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(top)
    assert "than the limit (500 steps)" in str(excinfo.value), str(excinfo.value)


def test_the_check_rejects_unbalanced_blocks_itself():
    """事前檢查自己就驗區塊平衡——建立排程／監看時只呼叫它，不經過直譯器。"""
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(["repeat 2", "wait 0"])
    assert "no matching" in str(excinfo.value)
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(["end"], name="m")
    assert str(excinfo.value).startswith("Macro `m`: "), str(excinfo.value)


def test_the_check_names_the_macro_and_the_line(macro_dir):
    del macro_dir
    gui.save_macro("inner", ["wait 0", "type $2"])
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(["wait 0", "call inner a"], name="outer")
    assert str(excinfo.value) == (
        "Macro `outer`: Line 2: Macro `inner`: Line 2: Wrong number of arguments for `type`.")
    # 不合巨集名稱規則的 `name` 不印進訊息（訊息會原樣送給對話平台）
    with pytest.raises(GuiError) as excinfo:
        gui.check_macro_program(["type $1"], name="../evil path")
    assert str(excinfo.value) == "Line 1: Wrong number of arguments for `type`."


# --------------------------------------------------------------------------
# `$$`：字面的 `$`（2026-09-21）
#
# 沒有跳脫的時候，巨集裡寫不出「`$` 後面接數字」，而錄製下來的 `$100` 重播時會
# 打出 `00`——安靜的資料損毀。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("step, args, expected", [
    ("type $$", [], "type $"),
    ("type $$1", ["a"], "type $1"),           # 跳脫之後的 `1` 不是參數
    ("type $$$1", ["a"], "type $a"),          # 由左往右配對
    ("type $$$$", [], "type $$"),
    ("type a$b", [], "type a$b"),             # 落單的 `$` 照原樣
    ("type x$", [], "type x$"),               # 結尾的 `$` 照原樣
    ("type $0", ["a"], "type $0"),            # `$0` 不是參數
    ("type $10", ["a"], "type a0"),           # 只看一位數
    ("type $1 and $2", ["a", "b"], "type a and b"),
    ("type $3", ["a"], "type "),              # 沒給的換成空字串
])
def test_the_substitution_grammar(step, args, expected):
    assert gui.substitute_macro_args(step, args) == expected


@pytest.mark.parametrize("value", ["$1", "$$", "$2$$1", "a$", "$9x"])
def test_argument_values_are_never_rescanned(value, macro_spy):
    """代入只做一遍：參數值裡的 `$1` / `$$` 原樣送出，不會變成另一個參數或被折半。"""
    assert gui.substitute_macro_args("type $1 $2", [value, "B"]) == f"type {value} B"
    gui.run_macro_program(["type $1"], args=[value], should_abort=_never_abort)
    assert macro_spy.calls == [("type_text", (value,), {})]


@pytest.mark.parametrize("text", ["$100", "$$", "a$b", "x$", "$$1", "$1$", "$"])
def test_escaping_round_trips_through_substitution(text):
    escaped = gui.escape_macro_text(text)
    for args in ([], ["ZZ", "YY"]):
        assert gui.substitute_macro_args(escaped, args) == text, (text, args)


# `$` 在 shift+4；數字與幾個字母不按 shift
_DOLLAR_TABLE = {0x34: ("4", "$"), 0x31: ("1", "!"), 0x30: ("0", ")"),
                 0x41: ("a", "A"), 0x42: ("b", "B"), 0x58: ("x", "X")}
_DOLLAR_KEYS = {"$": (0x34, True), "1": (0x31, False), "0": (0x30, False),
                "a": (0x41, False), "b": (0x42, False), "x": (0x58, False)}


def _typing_events(text: str) -> list[dict]:
    events, clock = [], 0.0
    for char in text:
        vk, shift = _DOLLAR_KEYS[char]
        if shift:
            events.append(_event("kdown", clock, vk=16))
        events.append(_event("kdown", clock + 0.001, vk=vk))
        if shift:
            events.append(_event("kup", clock + 0.002, vk=16))
        clock += 0.01
    return events


@pytest.mark.parametrize("typed", ["$100", "$$", "a$b", "x$", "$1"])
def test_a_recorded_dollar_replays_verbatim(fake_ac, macro_spy, typed):
    """錄到的 `$` 寫成 `$$`，重播打出來的還是當時打的那個字——給了參數也一樣。"""
    steps = gui.record_to_steps(_typing_events(typed), char_table=_DOLLAR_TABLE)
    assert steps == [f"type {typed.replace('$', '$$')}"], steps
    for args in ([], ["ZZ"]):
        macro_spy.reset()
        gui.run_macro_program(steps, args=args, should_abort=_never_abort)
        assert macro_spy.calls == [("type_text", (typed,), {})], (args,
                                                                   macro_spy.calls)


# --------------------------------------------------------------------------
# 衍生守門：事前檢查與逐步執行對「代入後的程式」必須讀出同一個答案（2026-09-21）
# --------------------------------------------------------------------------
# 上面那一節逐案釘住已知的形狀；這一支從 `MACRO_VERBS` **衍生**，每一個帶參數的動詞
# （包括以後新增的）自動納入：取它一步合法的寫法，把其中一個參數換成 `$1`，前面墊一個
# `click 10 10` 當哨兵，再拿一池敵意參數值去跑。兩個讀者、兩個方向——
#
# * 事前檢查**拒收** → 一個動作都不能送出（哨兵那一下點選也不行）。舊的缺陷正是
#   「逐步代入、逐步驗證」，壞掉的第二行被發現時第一行已經點下去了。
# * 事前檢查**放行** → 逐步執行不得在半路失敗。檢查少走了一種步驟（例如沒跟進
#   `call`、驗的是代入前的字面）的話，會在這個方向現形。
#
# 事前檢查與執行端共用 `validate_macro_step`，所以**那支解析函式本身**壞掉時兩邊
# 一起動，這裡看不到——那是上面逐案測試與 `test_every_step_the_validator_accepts…`
# 的範圍。


_PRECHECK_VALUES = ("", "0", "-1", "2.5", "150", "nan", "#ffffff", "存檔", "left",
                    "ctrl+s", "leaf", "child", "$1", "$$")
# 從 `MACRO_VERBS` 衍生的樣板只會產生 `call $1`（被呼叫者本身是參數），產生不出
# 「參數傳給被呼叫者」這個形狀。`wait_child` 不給參數時合法、給 `150` 時不合法，
# 所以事前檢查若把參數弄丟，就會放行一個執行到一半才失敗的程式。
_PRECHECK_EXTRA_TEMPLATES = {"call": [["click 10 10", "call wait_child $1"]]}
# 兩個方向的下限：2026-09-21 量到拒收 800、放行 1,630，往下取整留餘裕。
_PRECHECK_REJECTED_FLOOR = 700
_PRECHECK_ACCEPTED_FLOOR = 1500


def _macro_precheck_templates(verbs, validates, pool, tail_pool):
    """回 `{動詞: [樣板程式…]}`。每個參數個數取第一個合法寫法，逐一把參數換成 `$1`。"""
    templates: dict[str, list[list[str]]] = {}
    for verb, (low, high) in sorted(verbs.items()):
        seen: list[list[str]] = []
        top = min(high if high is not None else low + 2, 4)
        for count in range(max(low, 1), top + 1):
            base = next((step for step in _macro_corpus(verb, count, count, pool, tail_pool)
                         if validates(step)), None)
            if base is None:
                continue
            tokens = base.split()
            for position in range(1, len(tokens)):
                body = " ".join([*tokens[:position], "$1", *tokens[position + 1:]])
                program = ["click 10 10", body]
                if verb == "repeat" or verb in gui.MACRO_CONDITIONS:
                    program.append("end")
                if program not in seen:
                    seen.append(program)
        templates[verb] = seen
    return templates


def _macro_precheck_problems(cases, check, run):
    """回 `({動詞: {"rejected": n, "accepted": m}}, 問題清單)`。

    `cases` 是 `(動詞, 程式, 參數)`；`check(steps, args)` 拒收就丟 `GuiError`；
    `run(steps, args)` 回 `(送出的動作清單, 執行時丟的 GuiError 或 None)`。
    """
    counts: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    for label, steps, args in cases:
        tally = counts.setdefault(label, {"rejected": 0, "accepted": 0})
        try:
            check(steps, args)
        except GuiError:
            tally["rejected"] += 1
            actions, _error = run(steps, args)
            if actions:
                problems.append(
                    f"{steps!r} 參數 {args!r}：事前檢查拒收，卻已經送出 {actions}")
            continue
        tally["accepted"] += 1
        _actions, error = run(steps, args)
        if error is not None:
            problems.append(
                f"{steps!r} 參數 {args!r}：事前檢查放行，執行到一半卻失敗：{error}")
    return counts, problems


def test_the_precheck_parity_check_can_actually_see_both_disagreements():
    """合成對照組：兩個方向各一個會報、各一個不會報的例子。

    真實樹是乾淨的，所以「報出問題」那兩行在真實資料上從來不執行——刪掉任何一行
    整個守門照樣綠。
    """
    def check(steps, args):
        if steps[0].startswith("bad"):
            raise GuiError("拒收。")

    outcomes = {
        "bad-acts": (["mouse_click"], GuiError("晚了")),   # 拒收卻動了 → 報
        "bad-quiet": ([], GuiError("擋下")),               # 拒收、沒動 → 不報
        "ok-breaks": (["mouse_click"], GuiError("半路")),  # 放行卻半路失敗 → 報
        "ok-runs": (["mouse_click"], None),                 # 放行、跑完 → 不報
    }

    def run(steps, args):
        return outcomes[steps[0]]

    counts, problems = _macro_precheck_problems(
        [(name.split("-")[0], [name], []) for name in outcomes], check, run)
    assert counts == {"bad": {"rejected": 2, "accepted": 0},
                      "ok": {"rejected": 0, "accepted": 2}}, counts
    assert len(problems) == 2, problems
    assert "bad-acts" in problems[0] and "已經送出" in problems[0], problems
    assert "ok-breaks" in problems[1] and "半路" in problems[1], problems


def test_the_precheck_and_the_executor_agree_on_every_substituted_program(
        stubbed_desktop, macro_spy, macro_dir):
    del macro_dir
    tripwire = stubbed_desktop
    gui.save_macro("leaf", ["type x"])
    gui.save_macro("child", ["type $1"])      # 沒給參數就壞：`call child` 必須被事前擋下
    gui.save_macro("wait_child", ["wait_ui $1 存檔"])

    def check(steps, args):
        gui.check_macro_program(steps, args)

    def run(steps, args):
        macro_spy.reset()
        tripwire.touched.clear()
        try:
            gui.run_macro_program(steps, args=args, should_abort=_never_abort)
        except GuiError as error:
            return macro_spy.names + tripwire.touched, error
        return macro_spy.names + tripwire.touched, None

    templates = _macro_precheck_templates(
        gui.MACRO_VERBS, _validates, _MACRO_PARITY_POOL, _MACRO_PARITY_TAIL_POOL)
    takes_args = {verb for verb, (_low, high) in gui.MACRO_VERBS.items()
                  if high is None or high >= 1}
    silent = sorted(verb for verb in takes_args if not templates.get(verb))
    assert not silent, (
        f"生不出這些動詞的樣板，等於沒檢查：{silent}；把它需要的參數形狀加進"
        "`_MACRO_PARITY_POOL`。")
    for verb, extra in _PRECHECK_EXTRA_TEMPLATES.items():
        templates.setdefault(verb, []).extend(extra)
    cases = [(verb, program, args)
             for verb, programs in templates.items() for program in programs
             for args in ([], *([value] for value in _PRECHECK_VALUES))]
    counts, problems = _macro_precheck_problems(cases, check, run)
    # 兩個方向都要真的被走到（下限是量過的數字往下取整，不是猜的）。總量之外還要
    # **逐個動詞**至少放行一次：少了這一條，樣板組錯（例如區塊忘了補 `end`）會讓
    # 整類動詞兩邊一起拒收——兩個讀者一致、守門全綠，而那一類其實沒被檢查。
    rejected = sum(tally["rejected"] for tally in counts.values())
    accepted = sum(tally["accepted"] for tally in counts.values())
    assert rejected >= _PRECHECK_REJECTED_FLOOR, (rejected, accepted)
    assert accepted >= _PRECHECK_ACCEPTED_FLOOR, (rejected, accepted)
    never = sorted(verb for verb, tally in counts.items() if not tally["accepted"])
    assert not never, f"這些動詞的樣板一次都沒被放行過，等於只檢查了拒收那一半：{never}"
    assert not problems, (
        "事前檢查與逐步執行對同一個代入後的程式讀出不同答案（前者拒收卻已經動了，"
        "或前者放行、後者卻在半路失敗）：\n" + "\n".join(problems[:10]))


# --------------------------------------------------------------------------
# 覆蓋率盤點補的測試（2026-09-21）：整套測試跑完仍然一行都沒執行過的路徑
# --------------------------------------------------------------------------
# 量法：全套件 `--cov-branch`，按函式排未執行的敘述。多數是對函式庫的薄包裝，這裡只
# 補**有行為要釘**的幾條：錄製的滾輪合併與孤兒放開、步數上限與結尾 `wait`、背景作業
# 輸出的記憶體上限、寫檔失敗的收尾、`run_shell` 的兩條失敗路徑、`_kill_tree` 的容錯。
# 共通點是「平常不走、出事才走」——正是沒有人會手動試到的那一種。

def test_record_to_steps_merges_a_chain_of_wheel_events(fake_ac):
    """連續滾動併成一步；判斷看的是**跟上一格**的間隔，所以一串每格 0.25 秒的滾動
    整串併起來（總長超過 0.3 秒也一樣），停久了才另起一步並補 `wait`。"""
    events = [
        _event("wheel", 0.0, delta=-1),
        _event("wheel", 0.25, delta=-2),
        _event("wheel", 0.5, delta=-1),
        _event("wheel", 1.5, delta=2),
    ]
    assert gui.record_to_steps(events) == ["scroll -4", "wait 1.5", "scroll 2"]


def test_record_to_steps_ignores_a_release_whose_press_it_never_saw(fake_ac):
    """錄製開始時滑鼠鍵已經按著：只錄到放開。那不是一次點選，不得憑空生出 `click`。"""
    events = [
        _event("mup", 0.0, button="left", x=5, y=5),
        _event("kdown", 1.0, vk=0x48),
    ]
    assert gui.record_to_steps(events, char_table=_US) == ["type h"]


def test_record_to_steps_drops_a_step_that_fails_validation_and_says_so(fake_ac, capsys):
    """轉出來卻驗不過的一步（這裡是不認得的滑鼠鍵）要丟掉並在 stderr 留一行——混進去的話
    `load_macro` 會拒收**整個**巨集，而不只是那一步。"""
    events = [
        _event("mdown", 0.0, button="bogus", x=5, y=5),
        _event("mup", 0.05, button="bogus", x=5, y=5),
        _event("mdown", 0.1, button="left", x=90, y=90),   # 離得夠遠，不會被併成雙擊
        _event("mup", 0.15, button="left", x=90, y=90),
    ]
    assert gui.record_to_steps(events) == ["click 90 90"]
    assert "dropped unrecordable step 'click 5 5 bogus'" in capsys.readouterr().err


def test_record_to_steps_never_ends_on_a_wait_after_dropping_the_last_step(fake_ac):
    """最後一步被丟掉（叫不出名字的鍵）之後，留下的 `wait` 就成了結尾——後面沒有東西好等，
    重播只會平白多停那麼久。"""
    events = [
        _event("mdown", 0.0, button="left", x=1, y=1),
        _event("mup", 0.02, button="left", x=1, y=1),
        _event("kdown", 2.0, vk=0xFF),       # 叫不出名字 → `# 未知按鍵`，驗證前就丟掉
    ]
    assert gui.record_to_steps(events) == ["click 1 1"]


def test_record_to_steps_stops_at_the_step_cap_without_a_trailing_wait(fake_ac, monkeypatch):
    """步數上限要守住；剛好截在一個 `wait` 上時，那個 `wait` 也要拿掉。"""
    monkeypatch.setattr(gui, "MACRO_MAX_STEPS", 4)
    events = []
    for index in range(5):
        start = float(index)
        events.append(_event("mdown", start, button="left", x=index * 20, y=0))
        events.append(_event("mup", start + 0.02, button="left", x=index * 20, y=0))
    steps = gui.record_to_steps(events)
    assert steps == ["click 0 0", "wait 1", "click 20 0"], steps


class _ScriptedJobProc:
    """給 `_job_reader` 的假行程：stdout 是一串固定的行，可選擇在某一行之後炸掉。"""

    def __init__(self, lines, *, explode_after=None, wait_raises=False):
        self._lines = list(lines)
        self._explode_after = explode_after
        self._wait_raises = wait_raises
        self.returncode = 3
        self.closed = []
        self.stdout = self
        self.stderr = None
        self.stdin = None

    def __iter__(self):
        for index, line in enumerate(self._lines):
            if self._explode_after is not None and index == self._explode_after:
                raise OSError("pipe broke")
            yield line

    def wait(self):
        if self._wait_raises:
            raise RuntimeError("wait failed")
        return self.returncode

    def close(self):
        self.closed.append("stdout")


def _job_record(proc):
    return {"id": 99, "proc": proc, "lines": [], "total_lines": 0, "dropped": 0}


def test_a_chatty_job_keeps_only_the_newest_lines_and_counts_the_rest(monkeypatch):
    """一個話多的建置可以吐出幾十萬行；只留最近的 `JOB_LOG_MAX_LINES` 行，丟掉的另外記數，
    `/proc job log` 才不會讓人以為看到了全部。"""
    monkeypatch.setattr(gui, "JOB_LOG_MAX_LINES", 3)
    job = _job_record(_ScriptedJobProc([f"line {n}\n" for n in range(10)]))
    gui._job_reader(job)
    assert job["lines"] == ["line 7", "line 8", "line 9"]
    assert job["dropped"] == 7
    assert job["total_lines"] == 10
    assert job["rc"] == 3 and job["finished"]


def test_a_job_reader_that_breaks_midway_still_records_the_exit(monkeypatch, capsys):
    """讀到一半管道壞掉：已讀到的行要留著、結束碼與結束時間照樣寫上（`finally`），
    否則 `/proc job list` 會永遠顯示這個作業還在跑。"""
    job = _job_record(_ScriptedJobProc(["a\n", "b\n", "c\n"], explode_after=2,
                                       wait_raises=True))
    gui._job_reader(job)
    assert job["lines"] == ["a", "b"]
    assert job["rc"] == 3 and job["finished"]
    assert "reader failed" in capsys.readouterr().err


def test_write_host_file_to_a_folder_needs_a_file_name(tmp_path):
    with pytest.raises(GuiError, match="please give a full file name"):
        gui.write_host_file(str(tmp_path), b"x")
    with pytest.raises(GuiError, match="please give a full file name"):
        gui.write_host_file(str(tmp_path / "not-yet") + "\\", b"x")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("unlink_also_fails", [False, True])
def test_a_failed_host_write_leaves_neither_temp_nor_target(tmp_path, monkeypatch,
                                                            unlink_also_fails):
    """`os.replace` 失敗：暫存檔要清掉、目的檔不得出現，回的是本專案寫的那一句（不是
    `OSError` 原文——它帶著主機路徑）。清暫存檔本身也失敗時，照樣回那一句而不是讓
    第二個 `OSError` 穿出去。"""
    target = tmp_path / "out.txt"

    def broken_replace(src, dst):
        raise OSError(f"cannot replace {dst}")

    monkeypatch.setattr(gui.os, "replace", broken_replace)
    if unlink_also_fails:
        real_unlink = type(target).unlink

        def broken_unlink(self, missing_ok=False):
            if self.name.endswith(".tmp"):
                raise OSError("locked")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(type(target), "unlink", broken_unlink)
    with pytest.raises(GuiError) as excinfo:
        gui.write_host_file(str(target), b"payload")
    assert str(excinfo.value) == "Failed to write the file."
    assert not target.exists()
    if not unlink_also_fails:
        assert not (tmp_path / "out.txt.tmp").exists()


def test_run_shell_reports_an_interpreter_that_will_not_start(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no such interpreter at C:\\secret\\path")

    monkeypatch.setattr(gui.subprocess, "Popen", refuse)
    with pytest.raises(GuiError) as excinfo:
        gui.run_shell("echo hi")
    assert str(excinfo.value) == "Could not start the command interpreter."


class _ShellProc:
    def __init__(self, failures):
        self._failures = list(failures)
        self.returncode = 7
        self.pid = 4242

    def communicate(self, timeout=None):
        if self._failures:
            raise self._failures.pop(0)
        return "", None


def test_run_shell_kills_the_tree_when_communicate_fails_unexpectedly(monkeypatch):
    """不是逾時的意外（管道壞掉之類）：一樣要砍整棵行程樹、從 `_SHELL_PROCS` 拿掉，
    回本專案寫的那一句。"""
    proc = _ShellProc([RuntimeError("pipe")])
    killed = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(gui, "_kill_tree", killed.append)
    with pytest.raises(GuiError) as excinfo:
        gui.run_shell("echo hi")
    assert str(excinfo.value) == "An error occurred while running the command."
    assert killed == [proc]
    assert proc not in gui._SHELL_PROCS


def test_run_shell_timeout_survives_a_second_communicate_failure(monkeypatch):
    """逾時之後砍樹、再收一次殘餘輸出；那一次也失敗時，回報的是「逾時」而不是例外。"""
    proc = _ShellProc([subprocess.TimeoutExpired("x", 1), RuntimeError("gone")])
    killed = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(gui, "_kill_tree", killed.append)
    result = gui.run_shell("echo hi", timeout=1)
    assert result["timed_out"] is True
    assert result["output"] == ""
    assert result["rc"] == 7
    assert killed == [proc]


class _FakeTreeProc:
    def __init__(self, calls, name, *, terminate_raises=False, kill_raises=False):
        self._calls = calls
        self.name = name
        self._terminate_raises = terminate_raises
        self._kill_raises = kill_raises

    def terminate(self):
        self._calls.append(("terminate", self.name))
        if self._terminate_raises:
            raise RuntimeError("access denied")

    def kill(self):
        self._calls.append(("kill", self.name))
        if self._kill_raises:
            raise RuntimeError("access denied")


def _fake_psutil(calls, children, alive, *, process_raises=False):
    module = types.ModuleType("psutil")

    class _Parent:
        def __init__(self, pid):
            if process_raises:
                raise RuntimeError("no such process")
            calls.append(("parent", pid))

        def children(self, recursive=False):
            assert recursive is True, "只殺直接子行程會漏掉孫行程"
            return children

    def wait_procs(procs, timeout=None):
        calls.append(("wait", tuple(p.name for p in procs)))
        return [p for p in procs if p not in alive], list(alive)

    module.Process = _Parent
    module.wait_procs = wait_procs
    return module


def test_kill_tree_keeps_going_past_every_child_that_resists(monkeypatch):
    """一個子行程 terminate 失敗、撐過等待、kill 又失敗——後面的步驟一個都不能少，
    最後直接子行程本身一定要被 kill。"""
    calls = []
    stubborn = _FakeTreeProc(calls, "stubborn", terminate_raises=True, kill_raises=True)
    polite = _FakeTreeProc(calls, "polite")
    monkeypatch.setitem(sys.modules, "psutil",
                        _fake_psutil(calls, [stubborn, polite], [stubborn]))
    root = _FakeTreeProc(calls, "root")
    root.pid = 1234
    gui._kill_tree(root)
    assert calls == [("parent", 1234), ("terminate", "stubborn"), ("terminate", "polite"),
                     ("wait", ("stubborn", "polite")), ("kill", "stubborn"),
                     ("kill", "root")]


def test_kill_tree_still_kills_the_child_when_the_tree_walk_fails(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "psutil",
                        _fake_psutil(calls, [], [], process_raises=True))
    root = _FakeTreeProc(calls, "root", kill_raises=True)
    root.pid = 1234
    gui._kill_tree(root)          # 連最後那個 kill 也失敗：不得有例外穿出去
    assert calls == [("kill", "root")]


# --------------------------------------------------------------------------
# 覆蓋率盤點找到的四個缺陷（2026-09-21）：修正與它們的守門
# --------------------------------------------------------------------------
class _StubbornAC(_FakeAC):
    """放開某些鍵會丟例外的假後端；`stuck` 清空之後就恢復正常。

    放開失敗時照樣記一筆 `("up!", 名稱)`，這樣測得出「有沒有真的去放」與「放了
    幾次」——重試次數是這一組缺陷的核心。
    """

    def __init__(self, stuck):
        super().__init__()
        self.stuck = set(stuck)

    def release_keyboard_key(self, key):
        if key in self.stuck:
            self.events.append(("up!", key))
            raise OSError("SendInput returned 0")
        super().release_keyboard_key(key)

    def release_mouse(self, button, x=None, y=None):
        if button in self.stuck:
            self.events.append(("mup!", button))
            raise OSError("SendInput returned 0")
        super().release_mouse(button, x, y)


@pytest.fixture(name="stubborn_ac")
def _stubborn_ac_fixture(monkeypatch):
    fake = _StubbornAC({"control"})
    monkeypatch.setattr(gui, "_AC", fake)
    monkeypatch.setattr(gui, "_AC_TRIED", True)
    gui._HELD_INPUTS.clear()
    try:
        yield fake
    finally:
        gui._HELD_INPUTS.clear()


def test_a_key_whose_release_failed_stays_registered_until_a_retry_works(stubborn_ac):
    """放開失敗的那一個要**留在登記裡、按下時間不變**；其餘照放。後端恢復之後，
    下一次全部放開要真的再放它一次。

    修正前 `_release_keys` 不管成功與否都把登記拿掉：`/input key status` 說沒按住
    任何鍵、`/input key clear` 的計數少一個、逾時計時器也找不到它——鍵卻還按著。
    """
    gui.key_down("shift")
    gui.key_down("ctrl")
    gui.mouse_button_down("mouse_left")
    pressed_at = gui.input_pressed_at("key", "control")

    released, stuck = gui.release_all_inputs_report()
    assert sorted(released) == ["mouse_left", "shift"]
    assert stuck == ["control"]
    assert [row[1] for row in gui.held_inputs()] == ["control"]
    assert gui.input_pressed_at("key", "control") == pressed_at, "按下時間被改掉了"

    stubborn_ac.stuck.clear()
    assert gui.release_all_inputs() == ["control"]
    assert gui.held_inputs() == []
    assert stubborn_ac.events.count(("up!", "control")) == 1
    assert stubborn_ac.events.count(("up", "control")) == 1, "恢復之後沒有再放一次"


def test_release_all_inputs_leaves_the_failed_key_out_of_its_count(stubborn_ac):
    """只回名單的那一支：放不掉的不算「已放開」，而且照樣留在登記裡。"""
    gui.key_down("ctrl")
    gui.key_down("w")
    assert gui.release_all_inputs() == ["w"]
    assert [row[1] for row in gui.held_inputs()] == ["control"]


def test_a_mouse_button_whose_release_failed_stays_registered(stubborn_ac):
    stubborn_ac.stuck = {"mouse_right"}
    gui.mouse_button_down("mouse_right")
    assert gui.release_all_inputs_report() == ([], ["mouse_right"])
    assert [row[:2] for row in gui.held_inputs()] == [("mouse", "mouse_right")]


def test_the_macro_end_release_keeps_a_key_whose_release_failed(stubborn_ac):
    """巨集收尾（`release_added_since`）同一條規則：放不掉的留著，下一次收尾再放。"""
    snapshot = gui.held_snapshot()
    gui.key_down("ctrl")
    gui.key_down("shift")
    assert gui.release_added_since(snapshot) == ["shift"]
    assert [row[1] for row in gui.held_inputs()] == ["control"]
    stubborn_ac.stuck.clear()
    assert gui.release_added_since(snapshot) == ["control"]
    assert gui.held_inputs() == []


def test_a_macro_that_ends_with_a_stuck_key_leaves_it_visible(stubborn_ac):
    """走真的巨集直譯器：收尾放不掉的鍵要看得到，而不是被當成已經放開。"""
    gui.run_macro(["keydown ctrl"])
    assert ("down", "control") in stubborn_ac.events
    assert ("up!", "control") in stubborn_ac.events, "收尾根本沒有去放"
    assert [row[1] for row in gui.held_inputs()] == ["control"]


def test_a_failed_stale_release_can_be_retried_with_the_same_press_time(stubborn_ac):
    """逾時計時器拿**同一個** `pressed_at` 再叫一次就是重試——前提是失敗時按下時間
    沒被動過。呼叫端靠「之後 `input_pressed_at` 還等不等於 `pressed_at`」分辨放開
    失敗與「不是同一次按住」。"""
    gui.key_down("ctrl")
    pressed_at = gui.input_pressed_at("key", "control")
    assert gui.release_input_if_stale("key", "control", pressed_at) is False
    assert gui.input_pressed_at("key", "control") == pressed_at
    stubborn_ac.stuck.clear()
    assert gui.release_input_if_stale("key", "control", pressed_at) is True
    assert gui.held_inputs() == []


def test_releasing_without_a_backend_forgets_nothing(monkeypatch):
    """後端載不進來時什麼都不拿掉、全部算放不掉。

    正式執行時這條走不到（登記只在按下成功之後才寫入，而那代表後端早就載入過），
    但原本的 `clear()` 會在走得到的那一種情形（快取被重設）把可能還按著的鍵一起忘掉。
    """
    def _no_backend():
        raise GuiError("桌面控制功能未安裝或無法在此環境使用。")

    monkeypatch.setattr(gui, "load_ac", _no_backend)
    monkeypatch.setattr(gui, "_HELD_INPUTS",
                        {("key", "shift"): 1.0, ("mouse", "mouse_left"): 2.0})
    assert gui.release_all_inputs_report() == ([], ["shift", "mouse_left"])
    assert gui.release_all_inputs() == []
    assert gui._HELD_INPUTS == {("key", "shift"): 1.0, ("mouse", "mouse_left"): 2.0}


def _click(t, x, y, button="left", *, hold=0.0):
    """一次點選的按下／放開兩個事件。預設按下即放，等待的秒數才算得整齊。"""
    return [_event("mdown", t, button=button, x=x, y=y),
            _event("mup", t + hold, button=button, x=x, y=y)]


@pytest.mark.parametrize("first, second, expected", [
    # 右鍵接著左鍵：兩次不同的點選。修正前是 `["dclick 9 9"]`——右鍵那一下不見了，
    # 還多出一次左鍵雙擊。
    ("right", "left", ["click 5 5 right", "click 9 9"]),
    ("left", "right", ["click 5 5", "click 9 9 right"]),
    # 同一顆鍵照舊併成雙擊，後綴也要留著。
    ("middle", "middle", ["dclick 9 9 middle"]),
    ("left", "left", ["dclick 9 9"]),
])
def test_a_double_click_merges_only_clicks_of_the_same_button(fake_ac, first, second,
                                                               expected):
    # 時間與座標照原始回報的重現步驟：(5,5) 在 0／0.05、(9,9) 在 0.1／0.15。
    events = (_click(0.0, 5, 5, first, hold=0.05)
              + _click(0.1, 9, 9, second, hold=0.05))
    assert gui.record_to_steps(events) == expected


def test_a_scroll_group_that_cancels_out_records_nothing(fake_ac):
    """往下三格又往上三格：什麼都沒捲。修正前錄成 `scroll 1`（往上一格，從來沒發生
    過），外加它自己的 `wait`。那段時間要併進下一個動作前面的等待（5 秒，不是
    2 ＋ 3）。"""
    events = (_click(0.0, 1, 1)
              + [_event("wheel", 2.0, delta=-3), _event("wheel", 2.1, delta=3)]
              + _click(5.0, 50, 50))
    assert gui.record_to_steps(events) == ["click 1 1", "wait 5", "click 50 50"]


def test_a_recording_that_only_scrolls_back_and_forth_is_empty(fake_ac):
    events = [_event("wheel", 0.0, delta=2), _event("wheel", 0.1, delta=-2)]
    assert gui.convert_recording(events, char_table={}) == gui.RecordedMacro([], 0, 0)


def test_a_scroll_group_keeps_its_net_integer_sum(fake_ac):
    events = (_click(0.0, 1, 1)
              + [_event("wheel", 2.0, delta=-3), _event("wheel", 2.1, delta=1)])
    assert gui.record_to_steps(events) == ["click 1 1", "wait 2", "scroll -2"]


def _clicks(count, *, start=0.0, spacing=1.0):
    events = []
    for index in range(count):
        events += _click(start + index * spacing, index * 20, 0)
    return events


def test_the_step_cap_reports_how_many_actions_it_cut(fake_ac, monkeypatch):
    """上限截掉的**動作**數（不含 `wait`）。五次點選、上限 4 步：存下 2 次點選，
    其餘 3 次要算進 `truncated`——存下的加上截掉的，剛好是錄到的全部。"""
    monkeypatch.setattr(gui, "MACRO_MAX_STEPS", 4)
    result = gui.convert_recording(_clicks(5), char_table={})
    assert result.steps == ["click 0 0", "wait 1", "click 20 0"]
    assert result.truncated == 3
    assert result.unrecordable == 0
    saved = sum(1 for step in result.steps if not step.startswith("wait "))
    assert saved + result.truncated == 5


def test_a_recording_under_the_cap_reports_nothing_cut(fake_ac):
    result = gui.convert_recording(_clicks(3), char_table={})
    assert result == gui.RecordedMacro(
        ["click 0 0", "wait 1", "click 20 0", "wait 1", "click 40 0"], 0, 0)


def test_the_unrecordable_count_does_not_depend_on_the_step_cap(fake_ac, monkeypatch):
    """重播不了的步驟不管落在截斷處前後都算：同一段錄製，上限不同、略過的數目一樣。

    一個在前（叫不出名字的鍵）、一個在後（不認得的滑鼠鍵）——只算截斷處之前的
    寫法會在上限 4 時少算一個。
    """
    events = ([_event("kdown", 0.0, vk=0xFF)]
              + _clicks(5, start=1.0)
              + _click(9.0, 500, 500, "bogus"))
    loose = gui.convert_recording(events, char_table={})
    monkeypatch.setattr(gui, "MACRO_MAX_STEPS", 4)
    tight = gui.convert_recording(events, char_table={})
    assert loose.unrecordable == tight.unrecordable == 2
    assert loose.truncated == 0 and tight.truncated == 3


def test_record_to_steps_is_the_steps_of_convert_recording(fake_ac, monkeypatch):
    monkeypatch.setattr(gui, "MACRO_MAX_STEPS", 4)
    events = _clicks(5) + _click(9.0, 500, 500, "bogus")
    assert gui.record_to_steps(events, char_table={}) == \
        gui.convert_recording(events, char_table={}).steps


def test_the_release_keys_step_says_how_many_keys_it_could_not_release(stubborn_ac):
    """巨集的 `release_keys` 步驟：放不掉的鍵不算進「放開」，但要講出來。

    修正前敘述只有「放開全部按鍵（N 個）」，N 已經不含放不掉的那一個，卻一個字都
    沒提——巨集的進度看起來一切正常，而那個鍵還列在 `/input key status`、還按著。
    後半是反方向：後端恢復之後再跑一次，敘述要回到原本那一句（少了這半，「永遠附
    一句」也會綠）。
    """
    gui.key_down("ctrl")
    gui.key_down("shift")
    assert gui.run_macro_step("release_keys") == (
        "released all keys (1 released; 1 could not be released)")
    assert [row[1] for row in gui.held_inputs()] == ["control"], "放不掉的鍵從登記裡消失了"
    stubborn_ac.stuck.clear()
    assert gui.run_macro_step("release_keys") == "released all keys (1 released)"
    assert gui.held_inputs() == []
