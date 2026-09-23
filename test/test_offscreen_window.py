"""瀏覽器視窗永遠不在螢幕上——開窗旗標、`hide_browser_windows` 的行為、以及「正式程式碼
裡不再有任何最小化／最大化／帶到前面」。

擁有者要求（2026-09-22）：「確保不會回到前景關閉，這樣會短暫出現生成的圖片預覽，不要有任何
地方在前景出現生成的圖片預覽」。理由與實測在 `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`
上面那一段。獨立成一個檔是因為 `test_webrunner_shared.py` 的自帶 runner 只餵得出 `tmp_path`、
也不支援 `parametrize`，而這裡需要替換 `je_auto_control` 與逐一跑兩個變體。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _webrunner_shared as ws  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


class _FakeWindowLibrary:
    """`je_auto_control` 的替身：只有 `hide_browser_windows` 會碰的那幾個名字，全部記錄。"""

    def __init__(self, windows):
        # windows: {hwnd: {"pid": int, "rect": (l, t, r, b) | None, "iconic": bool,
        #                  "move_ok": bool}}
        self._windows = windows
        self.calls: list = []
        lib = self

        class _WM:
            @staticmethod
            def is_window_minimized(hwnd):
                return lib._windows[hwnd]["iconic"]

            @staticmethod
            def get_window_rect(hwnd):
                return lib._windows[hwnd]["rect"]

            @staticmethod
            def move_window(hwnd, x, y, width, height, repaint=True):
                lib.calls.append(("move", hwnd, x, y, width, height))
                return lib._windows[hwnd].get("move_ok", True)

            @staticmethod
            def minimize_window(hwnd):
                lib.calls.append(("minimize", hwnd))
                return True

            @staticmethod
            def show_window(hwnd, cmd):
                lib.calls.append(("show", hwnd, cmd))
                return True

            @staticmethod
            def set_foreground_window(hwnd):
                lib.calls.append(("foreground", hwnd))

        self.windows_window_manage = _WM()

    def windows_for_process_id(self, pid, titled_only=False):
        if pid == 666:
            raise RuntimeError("enumeration failed")
        return [(hwnd, "t") for hwnd, w in self._windows.items() if w["pid"] == pid]

    def minimize_windows_for_process(self, pid):
        self.calls.append(("minimize_process", pid))
        return 0


def _hide_with(monkeypatch, windows, pids):
    lib = _FakeWindowLibrary(windows)
    monkeypatch.setitem(sys.modules, "je_auto_control", lib)
    monkeypatch.setattr(ws, "find_browser_pids_for_profile", lambda dirs: set(pids))
    return ws.hide_browser_windows([Path("P")]), lib


def test_an_on_screen_browser_window_is_moved_off_screen_without_minimizing(monkeypatch):
    result, lib = _hide_with(monkeypatch, {
        1: {"pid": 10, "rect": (10, 13, 955, 1025), "iconic": False},        # 在螢幕上
        2: {"pid": 10, "rect": (-32000, -32000, -30080, -30920), "iconic": False},  # 已在外面
        3: {"pid": 11, "rect": (-32000, -32000, -31840, -31972), "iconic": True},   # 最小化
    }, [10, 11])
    assert result == (3, 0), result
    x, y = ws.OFFSCREEN_WINDOW_POSITION
    assert lib.calls == [("move", 1, x, y, 945, 1012)], (
        f"只該搬那個在螢幕上的視窗、保留原本大小，而且不得最小化／還原／帶到前面：{lib.calls}")


def test_a_window_that_could_not_be_hidden_is_reported_as_exposed(monkeypatch, capsys):
    result, lib = _hide_with(monkeypatch, {
        1: {"pid": 10, "rect": (0, 0, 800, 600), "iconic": False, "move_ok": False},
        2: {"pid": 10, "rect": None, "iconic": False},
    }, [10, 666])
    assert result == (0, 3), result          # 搬不動、讀不到位置、列舉失敗各算一個
    assert "hide_browser_windows(666) failed" in capsys.readouterr().err
    assert not [c for c in lib.calls if c[0] != "move"], lib.calls


def test_finding_no_window_is_not_reported_as_hidden(monkeypatch):
    """`(0, 0)` 是「一個都沒找到」，呼叫端不得把它讀成「藏好了」。"""
    result, _lib = _hide_with(monkeypatch, {}, [])
    assert result == (0, 0)
    for variant in ("webrunner_novelai", "webrunner_je_only"):
        module = __import__(variant)
        monkeypatch.setattr(module, "hide_browser_windows", lambda dirs: (0, 0))
        hide = module.hide_chrome_window
        assert (hide(None) if variant == "webrunner_novelai" else hide()) is False
        monkeypatch.setattr(module, "hide_browser_windows", lambda dirs: (2, 1))
        assert (hide(None) if variant == "webrunner_novelai" else hide()) is False
        monkeypatch.setattr(module, "hide_browser_windows", lambda dirs: (2, 0))
        assert (hide(None) if variant == "webrunner_novelai" else hide()) is True


def _launch_flags(module_name: str) -> list[str]:
    """兩個變體開 Chrome 的字面旗標（AST，不 import）：novelai 的 `add_argument` 常數、
    je 的 `cli_args` 常數。"""
    tree = ast.parse((PKG_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
    flags: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args
                and isinstance(node.args[0], ast.Constant)):
            flags.append(node.args[0].value)
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.List)
                and any(isinstance(t, ast.Name) and t.id == "cli_args" for t in node.targets)):
            flags.extend(e.value for e in node.value.elts
                         if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return flags


@pytest.mark.parametrize("module_name", ["webrunner_novelai", "webrunner_je_only"])
def test_both_variants_open_the_browser_off_screen(module_name):
    flags = _launch_flags(module_name)
    # 正面對照用一個一定在的旗標，而不是數量——novelai 那一側的記憶體旗標走迴圈、profile
    # 走 f-string，字面常數只有四個，數量下限是猜的（2026-09-22 第一版就猜錯了）。
    assert "--disable-blink-features=AutomationControlled" in flags, (
        f"抽不到 {module_name} 的開窗旗標，抽取器壞了：{flags}")
    x, y = ws.OFFSCREEN_WINDOW_POSITION
    width, height = ws.OFFSCREEN_WINDOW_SIZE
    assert f"--window-position={x},{y}" in flags, flags
    assert f"--window-size={width},{height}" in flags, flags
    assert "--start-maximized" not in flags and "--start-fullscreen" not in flags, (
        f"{module_name} 又開始用最大化開窗了——被喚回前景的那一下就看得到剛產出的圖")


_WINDOW_SHOWING_NAMES = frozenset({
    "minimize_window", "maximize_window", "fullscreen_window",
    "minimize_windows_for_process", "minimize_window_by_title",
    "show_window", "show_window_by_title", "set_foreground_window", "focus_window",
    "set_window_state",
})


def _window_showing_calls(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None))
            if name in _WINDOW_SHOWING_NAMES:
                found.append(f"{name}() L{node.lineno}")
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and "Page.bringToFront" in node.value):
            found.append(f"Page.bringToFront L{node.lineno}")
    return found


def test_the_window_showing_scan_actually_bites():
    """合成對照：每一種會把視窗叫到前面或最小化的寫法，掃描都要抓得到。"""
    sample = ast.parse(
        "port.minimize_window()\n"
        "driver.maximize_window()\n"
        "ac.minimize_windows_for_process(1)\n"
        "wm.show_window(h, 9)\n"
        "wm.set_foreground_window(h)\n"
        "driver.execute_cdp_cmd('Page.bringToFront', {})\n")
    assert len(_window_showing_calls(sample)) == 6, _window_showing_calls(sample)


@pytest.mark.parametrize("module_name",
                         ["webrunner_novelai", "webrunner_je_only", "_webrunner_shared"])
def test_no_production_path_minimizes_or_raises_the_browser_window(module_name):
    """最小化、最大化、還原、帶到前面——任何一個加回來，視窗就會再在前景閃一下。"""
    tree = ast.parse((PKG_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
    hits = _window_showing_calls(tree)
    assert not hits, (
        f"{module_name} 又出現會讓瀏覽器視窗回到螢幕上的呼叫：{hits}。視窗要一直待在螢幕外"
        "（`hide_browser_windows` 只搬、不最小化）；理由見 `OFFSCREEN_WINDOW_POSITION`。")
