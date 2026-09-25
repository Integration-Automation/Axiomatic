"""Desktop-automation primitives (bot-only helper, passive module).

`discord_bot.py`'s GUI-control commands (mouse / window / clipboard / screen /
text and image location / macro / shell) keep the "how to do it" here, leaving
only the "how to reply" in the bot.

Design constraints:

* **The automation itself is not implemented here.** Typing, text recognition
  and cross-word matching, template matching, the UI element tree, the DPI
  conversion for screenshots—all of it delegates to the desktop-automation
  library (`je_auto_control`, pointed at the source tree in editable mode
  locally). Whatever the library lacks is added to the library, not worked
  around here: with two implementations of the same thing, only one of them
  ever gets fixed. What this module keeps is **argument parsing**, **local
  environment policy** (recognition-engine path, language-data location,
  language selection), **de-identification**, and the **abort and timeout**
  handling that needs an event loop.
* **Does not import `discord` or `discord_bot`** (the latter would be circular).
  This module uses only stdlib plus the optional `je_auto_control` / `psutil` /
  `Pillow`, so it can be loaded directly by tests and reused by another process.
* **Every function is synchronous and blocking.** The bot is a single event
  loop, so the caller must wrap these in `asyncio.to_thread`; `wait_text` /
  `wait_window` / `run_shell` routinely take seconds to minutes, and running
  them on the loop directly would make the whole bot unresponsive.
* **Errors are always turned into `GuiError`, with messages hardcoded by this
  module** (generic, containing no host path / external service name / raw
  exception text), so the caller can send `str(error)` straight to the user
  without violating CLAUDE.md's hard Secrecy requirement. The real details are
  written to stderr by the caller.

**Multi-monitor trap**: `je_auto_control.screen_size()` returns only the primary
resolution, and `PIL.ImageGrab.grab()` also captures only the primary screen by
default, but mouse coordinates span the whole virtual desktop (with a secondary
monitor on the right, x exceeds the primary width). So `screen_info()` reports
both the primary screen and the virtual-desktop bounds, screenshots still
default to the primary screen, and capturing the whole virtual desktop must be
requested explicitly with `all`.
"""
from __future__ import annotations

import json
import math
import ntpath
import os
import re
import subprocess  # nosec B404 — the execution backend for `!sh`, owner-only at the caller
import sys
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACRO_DIR = PROJECT_ROOT / "macros"


class GuiError(Exception):
    """A user-visible failure. The message is guaranteed to be a generic string
    hardcoded by this module, safe to send straight to the chat platform."""


class GuiAborted(GuiError):
    """The user actively requested an abort.

    Deliberately subclasses `GuiError`: a caller that does not know this type
    still catches it and still gets a safe message. A caller that does know it
    (the macro executor) handles it separately—"was stopped" is not "failed",
    and reporting it as a failure would make `!macro stop` print a red line
    every time.
    """


# --------------------------------------------------------------------------
# Backend loading
# --------------------------------------------------------------------------
# Importing je_auto_control has a noticeable cost (it pulls in cv2 / numpy) and
# blows up outright in an environment with no desktop session. So load it lazily
# plus cache, and fold any failure into GuiError.
_AC: Any = None
_AC_TRIED = False


def load_ac() -> Any:
    """Return the je_auto_control module; raise `GuiError` if it cannot load."""
    global _AC, _AC_TRIED  # pylint: disable=global-statement
    if not _AC_TRIED:
        _AC_TRIED = True
        try:
            import je_auto_control as _module  # type: ignore
            _AC = _module
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] je_auto_control unavailable: {error!r}", file=sys.stderr)
            _AC = None
    if _AC is None:
        raise GuiError("Desktop control is not installed or unavailable in this environment.")
    return _AC


def _window_api():
    """Return the library's window-operations module; raise `GuiError` if not on
    Windows or it cannot load.

    This once switched to pywin32, because the library's `list_windows()`
    returned an hwnd that was a `ctypes.LP_c_long` pointer object rather than an
    int (`int(hwnd)` raised `ValueError`), and `close_window_by_title` actually
    did a minimise. Both have been fixed on the library side (the callback
    prototype was changed to `HWND`, and `close` now sends `WM_CLOSE` with a
    separate `minimize`), so this returns to a single implementation.
    """
    try:
        from je_auto_control.wrapper import auto_control_window  # type: ignore
        return auto_control_window
    except ImportError as error:
        raise GuiError("Window control is not installed (Windows only).") from error


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
COORD_MAX = 65535
HOTKEY_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_]+$")
MACRO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

MOUSE_BUTTONS = {
    "left": "mouse_left",
    "l": "mouse_left",
    "right": "mouse_right",
    "r": "mouse_right",
    "middle": "mouse_middle",
    "m": "mouse_middle",
    # Side buttons. Many programs bind "back / forward" to these two, and
    # without them a whole class of actions can only be simulated the long way
    # round with keyboard shortcuts.
    "x1": "mouse_x1",
    "back": "mouse_x1",
    "x2": "mouse_x2",
    "forward": "mouse_x2",
}


def parse_coord(raw: str, label: str) -> int:
    """Convert a single coordinate string to an int; raise `GuiError` if it is
    outside ±COORD_MAX.

    **Negatives are allowed**: coordinates span the whole virtual desktop, and
    with a secondary monitor placed to the left of or above the primary, the x /
    y of the upper half are naturally negative (measured locally, the virtual
    desktop starts at y = -164). Pinning the lower bound at 0 would make those
    positions unreachable forever.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError(f"{label} must be an integer.") from error
    if not -COORD_MAX <= value <= COORD_MAX:
        raise GuiError(f"{label} must be between -{COORD_MAX} and {COORD_MAX}.")
    return value


def parse_size(raw: str, label: str) -> int:
    """Width / height: must be a positive integer."""
    value = parse_coord(raw, label)
    if value <= 0:
        raise GuiError(f"{label} must be greater than 0.")
    return value


def parse_xy(parts: list[str]) -> tuple[int, int]:
    """`["500", "300"]` → `(500, 300)`."""
    if len(parts) != 2:
        raise GuiError("Two coordinate values are required: `<x> <y>`.")
    return parse_coord(parts[0], "x"), parse_coord(parts[1], "y")


def parse_button(raw: str) -> str:
    """Convert `left` / `r` / `middle` and the like into a je_auto_control button name."""
    key = (raw or "left").strip().lower()
    if key not in MOUSE_BUTTONS:
        raise GuiError(
            "The mouse button must be `left` / `right` / `middle` / `back` / `forward`.")
    return MOUSE_BUTTONS[key]


# The low-level key-name table uses the raw Win32 virtual-key names, which do
# not match what ordinary people (and all of this project's documentation)
# write: the table has **no** `ctrl` / `alt` / `enter` / `esc` / `win` /
# `backspace`, only `control` / `menu` / `return` / `escape` / `lwin` / `back`.
# Without this mapping layer, `!hotkey ctrl+s`, `!hotkey alt+f4`, `hotkey enter`
# in a macro—that is, every example in the docs—would all fail.
KEY_ALIASES = {
    "ctrl": "control", "lctrl": "lcontrol", "rctrl": "rcontrol",
    "alt": "menu", "lalt": "lmenu", "ralt": "rmenu",
    "enter": "return",
    "esc": "escape",
    "win": "lwin", "super": "lwin", "cmd": "lwin", "meta": "lwin",
    "backspace": "back", "bksp": "back",
    "del": "delete", "ins": "insert",
    "pgup": "prior", "pageup": "prior",
    "pgdn": "next", "pagedown": "next",
    "caps": "capital", "capslock": "capital",
    "printscreen": "snapshot", "prtsc": "snapshot", "prtscr": "snapshot",
    "scrolllock": "scroll",
    "numpad0": "num0", "numpad1": "num1", "numpad2": "num2",
    "numpad3": "num3", "numpad4": "num4", "numpad5": "num5",
    "numpad6": "num6", "numpad7": "num7", "numpad8": "num8",
    "numpad9": "num9",
    # These four target `_EXTRA_KEY_CODES` (the low-level table lacks them).
    # Microsoft defines these four as "the `+` `,` `-` `.` key for any
    # country/region", so these readable names do not lie; the `oem_1` family
    # varies by keyboard layout, so it is **deliberately given no** alias, see
    # `_EXTRA_KEY_CODES`.
    "plus": "oem_plus", "comma": "oem_comma",
    "minus": "oem_minus", "period": "oem_period",
}

# Virtual keys the low-level key-name table (192 entries) **cannot name**. The
# values are Microsoft's official "Virtual-Key Codes (Winuser.h)" definitions
# (checked 2026-09-21); the library's `press_keyboard_key` /
# `release_keyboard_key` / `hotkey` / `post_key_to_window` all take integer key
# codes, so this project uses names internally throughout (macro text,
# `_HELD_INPUTS`, replies) and only converts to integers with `_library_key`
# **at the moment of handing off to the library**.
#
# The consequence of not having this table (measured 2026-09-21): recording
# ctrl held while pressing `=` cannot be reverse-looked-up to a name when
# converting to steps → produces `# unknown key` → dropped before saving, so
# the recording reports "N steps" and replays one short; and `parse_key_name`
# consults the same table, so combinations like `hotkey ctrl+=` (zoom) and
# `ctrl+/` (comment) cannot even be written by hand.
#
# The naming rule is **honesty first**:
# * `oem_plus` / `oem_comma` / `oem_minus` / `oem_period`: Microsoft says "For any
#   country/region", independent of keyboard layout;
# * `oem_1`–`oem_8`, `oem_102`: Microsoft says "It can vary by keyboard"—so **do
#   not** name them `semicolon` / `slash` / `backtick` / `lbracket`, because those
#   names lie on a non-US layout (the same key prints a different character);
# * `oem_clear`: the low-level table lacks it too;
# * `launch_app2`: the low-level table **has** it, but only as uppercase
#   `LAUNCH_APP2`, and `parse_key_name` lowercases first, so that key can never
#   be written and would be dropped even when recorded;
# * `browser_home`: the low-level table is missing this one (its neighbours
#   `browser_favorites` and `volume_mute` are both present), so the "home" key
#   on a multimedia keyboard would be dropped when recorded.
#
# Names must be all lowercase, containing only letters, digits and underscores
# (`parse_key_name`'s literal rule), and must not share a name with the
# low-level table while pointing at a different key—both are tested.
_EXTRA_KEY_CODES: dict[str, int] = {
    "oem_1": 0xBA,
    "oem_plus": 0xBB,
    "oem_comma": 0xBC,
    "oem_minus": 0xBD,
    "oem_period": 0xBE,
    "oem_2": 0xBF,
    "oem_3": 0xC0,
    "oem_4": 0xDB,
    "oem_5": 0xDC,
    "oem_6": 0xDD,
    "oem_7": 0xDE,
    "oem_8": 0xDF,
    "oem_102": 0xE2,
    "oem_clear": 0xFE,
    "launch_app2": 0xB7,
    "browser_home": 0xAC,
}

# Names the low-level table **does** have, but pointing at the wrong key → the
# correct virtual-key code. This is a different thing from `_EXTRA_KEY_CODES`:
# that one adds keys the low-level table cannot name, this one overrides keys the
# low-level table names wrongly.
#
# `down`: the low level merged the mouse-event constants into the keyboard table,
# and `"down"` is `0x80` (the event flag for a mouse side-button press)—while
# `0x80` is also `VK_F17`. The "down" arrow key is only called `vk_down` (`0x28`)
# in that table; the other three directions `up` / `left` / `right` are all
# correct. So a hand-written `hotkey down`, `keydown down` or
# `/input key press down` was **silently** pressing F17 all along. F17 itself can
# still be written (`f17`).
#
# The real fix belongs in that upstream table (`wrapper/_platform_windows.py` in
# `<the local AutoControlGUI checkout>`, which is another project—**do not** go
# and change it). Upstream fixed it in the source tree on 2026-09-22, but **the
# package index's release does not have it yet** (0.0.222 is still 0x80), and a
# fresh clone installs the release—so this entry stays until after that release,
# at which point it is deleted together with the `_FIXED_UPSTREAM_AWAITING_RELEASE`
# entry in the tests. On the fixed table it agrees with the low level (both
# 0x28), so keeping it changes no behaviour.
#
# On 2026-09-21 all 19 names in the low-level table that are not virtual keys
# (mouse events, `KEYEVENTF_*` flags, `MapVirtualKey` type constants) were
# scanned, and only `down` is a name an ordinary person would type as a key; the
# rest (`move`, `leftup`, `wheel`, `xbutton1`…) are not keys on any keyboard and
# are deliberately left alone.
_LIBRARY_NAME_OVERRIDES: dict[str, int] = {
    "down": 0x28,
}


def _library_key(key: str | int) -> str | int:
    """This project's key name → the key code handed to the library. **Every**
    place that sends a key to the library must go through here.

    Names in the override table and the extra table are converted to integers
    (the former are named wrongly by the low-level table, the latter cannot be
    found in it); everything else is handed over as-is for the library to look up
    itself—that path already works, and keeps its original error behaviour.
    Integers pass through unchanged (the key codes the `write()` fallback path
    receives are already values from the library's table).

    The override table **must be checked first**: an overridden name also exists
    in the low-level table (just pointing at the wrong key), so handing it over
    as-is would have the low level resolve it to the wrong key.
    """
    if isinstance(key, str):
        if key in _LIBRARY_NAME_OVERRIDES:
            return _LIBRARY_NAME_OVERRIDES[key]
        return _EXTRA_KEY_CODES.get(key, key)
    return key


def _library_keys(keys: list[str]) -> list[str | int]:
    """The list version of `_library_key` (for key combinations)."""
    return [_library_key(key) for key in keys]


def parse_hotkey_tokens(raw: str) -> list[str]:
    """`"ctrl+shift+t"` → `["control", "shift", "t"]` (aliases applied and
    validated).

    Every token is looked up in the key-name table—without validation, a
    mistyped key name would only fail once it was actually sent, and the error
    message would become the low level's unpredictable internal string.
    """
    tokens = [t.strip() for t in (raw or "").strip().lower().split("+") if t.strip()]
    if not tokens:
        raise GuiError("Give a key combination, e.g. `ctrl+s`.")
    return [parse_key_name(token) for token in tokens]


def parse_duration(raw: str, *, maximum: float) -> float:
    """Seconds string → float, with a cap applied (so one `wait 99999` cannot tie
    up the worker thread).

    **`nan` must be blocked here; the two range checks below cannot be relied on
    alone.** `float("nan")` is a legal conversion, and every comparison with nan
    returns False, so `value < 0` and `value > maximum` **both let it through**—the
    same shape is already recorded in `_batch_config._is_finite_number`. The
    consequence of letting it through is not "a long wait" but **never
    finishing**: every wait loop in this module is written as
    `deadline = time.monotonic() + timeout` with
    `if time.monotonic() >= deadline`, and `x >= nan` is always False, so the
    timeout line never holds. Measured 2026-09-08: `wait_window(…, nan)` polled
    forty times and still had not timed out.

    The bot side is worse: the seconds for `/win wait`, `/locate text wait` and
    `/locate ui wait` also go through here, and those run in `asyncio.to_thread`
    **without an abort callback**—a stuck worker thread cannot be reclaimed by
    anyone, and `/macro stop` cannot reach it either.

    `inf` is already blocked by the cap (`inf > maximum` is True); only nan
    slipped through.
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError("Seconds must be a number.") from error
    if not math.isfinite(value):
        raise GuiError("Seconds must be a finite number.")
    if value < 0:
        raise GuiError("Seconds cannot be negative.")
    if value > maximum:
        raise GuiError(f"The maximum is {maximum:g} seconds.")
    return value


def parse_region(parts: list[str]) -> list[int]:
    """`["x", "y", "w", "h"]` → `[left, top, right, bottom]` (PIL bbox format).

    The user enters "top-left corner + width and height", because that is the
    most intuitive way to write it while looking at the screen; PIL wants two
    corners, so this converts. Width and height must be positive, otherwise
    `ImageGrab` returns a 0-pixel image that only blows up at save time.
    """
    if len(parts) != 4:
        raise GuiError("A region needs four values: `<x> <y> <width> <height>`.")
    left = parse_coord(parts[0], "x")
    top = parse_coord(parts[1], "y")
    width = parse_size(parts[2], "width")
    height = parse_size(parts[3], "height")
    return [left, top, left + width, top + height]


# --------------------------------------------------------------------------
# Abort-aware waiting
# --------------------------------------------------------------------------
# Every `wait_*` is "poll → sleep a little → poll again". Without this section,
# `!macro stop` claims to "take effect between steps", but in practice it could
# only stop once the current wait step ran to its timeout—`MACRO_MAX_WAIT_SEC` is
# 120 seconds, which means the abort command had two full minutes of no response.
#
# The sleep is cut into small slices rather than slept in one go: the abort
# response time is set by `_ABORT_POLL_SEC`, independent of the wait step's own
# polling interval (one round of `wait_text` takes three seconds, which should not
# make the abort three seconds slower too).
_ABORT_POLL_SEC = 0.2


def _check_abort(should_abort: Callable[[], bool] | None) -> None:
    """Raise `GuiAborted` if an abort was requested. `None` means nobody is
    watching, so let it through."""
    if should_abort is not None and should_abort():
        raise GuiAborted("Aborted as requested.")


def _sleep_abortable(seconds: float,
                     should_abort: Callable[[], bool] | None) -> None:
    """Sleep `seconds` seconds, checking for an abort every `_ABORT_POLL_SEC`."""
    if should_abort is None:
        time.sleep(max(0.0, seconds))
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _check_abort(should_abort)
        rest = deadline - time.monotonic()
        if rest <= 0:
            return
        time.sleep(min(_ABORT_POLL_SEC, rest))


# --------------------------------------------------------------------------
# Screen
# --------------------------------------------------------------------------
def virtual_bounds() -> tuple[int, int, int, int] | None:
    """The virtual desktop's `(x, y, width, height)`, in **logical (post DPI
    virtualisation) coordinates**.

    These numbers are exactly the coordinate space the mouse API uses. Returns
    None when unavailable (not Windows), and the caller falls back to looking at
    the primary screen only.
    """
    try:
        from je_auto_control.utils.monitor_layout import (  # type: ignore
            logical_virtual_rect,
        )
        return logical_virtual_rect()
    except Exception:  # pylint: disable=broad-except
        return None


def screen_info() -> dict[str, Any]:
    """Primary screen resolution + virtual desktop bounds + number of monitors.

    With multiple monitors, `!click` coordinates are virtual-desktop coordinates,
    so the user needs to see the virtual desktop's bounds to know where the
    secondary monitor's x starts. When virtual-desktop information is unavailable,
    only the primary screen is returned.
    """
    ac = load_ac()
    try:
        primary = list(ac.screen_size())
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read screen information.") from error
    info: dict[str, Any] = {
        "primary": (int(primary[0]), int(primary[1])),
        "virtual": virtual_bounds(),
        "monitors": 1,
    }
    try:
        from je_auto_control.utils.monitor_layout import (  # type: ignore
            enumerate_monitors,
        )
        info["monitors"] = max(1, len(enumerate_monitors()))
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass  # Cannot enumerate (not Windows, etc.): returning the primary screen is enough
    return info


def _grab_virtual_logical():
    """A screenshot of the whole virtual desktop, **converted into the mouse's
    coordinate space**.

    The conversion itself lives in the library's `grab_logical` (OCR and template
    matching share the same primitive). The pitfall it handles:
    `ImageGrab.grab(all_screens=True)` first makes itself DPI-aware and then
    grabs, returning **physical pixels**, while this process is DPI-unaware and
    the mouse API uses virtualised logical pixels—measured locally, logical
    3456×1244 vs screenshot 3840×1244 (secondary monitor at 125% scaling), so
    clicking an x=2500 counted off the screenshot lands 116 px off. The whole
    point of a screenshot is to let someone find where to click; if it does not
    line up, the feature is broken.
    """
    return _logical_frame(None)[0]


GIF_MAX_SECONDS = 15.0
GIF_MAX_FPS = 5.0
GIF_MAX_EDGE = 960


def capture_gif(dest: Path, *, seconds: float = 5.0, fps: float = 3.0,
                region: list[int] | None = None) -> int:
    """Shoot continuously for a while and save it as an animated GIF; return the
    frame count.

    A single screenshot cannot show a **process**: whether the progress bar is
    moving, which frame an animation is stuck on, what flashed on screen after a
    click. This fills that gap.

    The three caps are deliberate: duration, frame rate, and shrinking the long
    edge to `GIF_MAX_EDGE`. Forty-five unshrunk frames of a whole 3456×1244
    desktop come to several tens of MB, which cannot be sent and nobody wants to
    watch—motion matters more than sharpness, and anyone who really needs the
    detail should use `!screen` to capture that area anyway.
    """
    try:
        from PIL import Image  # type: ignore  # noqa: F401
    except ImportError as error:
        raise GuiError("Screenshot support is not installed.") from error
    span = max(0.5, min(float(seconds), GIF_MAX_SECONDS))
    rate = max(1.0, min(float(fps), GIF_MAX_FPS))
    interval = 1.0 / rate
    frames = []
    deadline = time.monotonic() + span
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            image, _ox, _oy = _logical_frame(region)
            if max(image.width, image.height) > GIF_MAX_EDGE:
                scale = GIF_MAX_EDGE / max(image.width, image.height)
                image = image.resize(
                    (max(1, int(image.width * scale)),
                     max(1, int(image.height * scale))))
            frames.append(image.convert("P", palette=1))
            rest = interval - (time.monotonic() - started)
            if rest > 0:
                time.sleep(rest)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Continuous capture failed.") from error
    if not frames:
        raise GuiError("No frames were captured.")
    try:
        frames[0].save(str(dest), save_all=True, append_images=frames[1:],
                       duration=int(interval * 1000), loop=0, optimize=True)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to save the continuous capture.") from error
    return len(frames)


def capture(dest: Path, *, region: list[int] | None = None,
            all_screens: bool = False) -> None:
    """Save a screenshot as PNG. `region` is `[left, top, right, bottom]`
    (logical coordinates).

    Capture goes through the library's `grab_logical`: it covers every monitor,
    converts the picture back into the click coordinate space, and does the
    region crop **on the converted image** too (`ImageGrab`'s `bbox` crops in
    physical-pixel space, which crops the wrong spot on a scaled monitor). It does
    not go through `je_auto_control.screenshot()`: that one is fixed to
    `ImageGrab.grab()` (primary screen only) and takes an extra detour through a
    cv2 colour-space conversion.
    """
    try:
        image, _origin_x, _origin_y = load_ac().grab_logical(
            _region_xywh(region), all_screens=(all_screens or region is not None))
        image.save(str(dest))
    except GuiError:
        raise                      # "the backend cannot load" is more useful than "screenshot failed"
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] capture failed: {error!r}", file=sys.stderr)
        raise GuiError("Screenshot failed.") from error


def pixel_color(x: int, y: int) -> tuple[int, int, int]:
    """Read a single pixel's colour, returning `(r, g, b)`.

    The low level returns inconsistent types across platforms (a COLORREF int on
    Windows, a tuple elsewhere), so this normalises it before returning.
    """
    ac = load_ac()
    try:
        raw = ac.get_pixel(x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the pixel colour.") from error
    if isinstance(raw, (tuple, list)) and len(raw) >= 3:
        return int(raw[0]), int(raw[1]), int(raw[2])
    if isinstance(raw, int):
        # A Win32 COLORREF is 0x00BBGGRR
        return raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF
    raise GuiError("Failed to read the pixel colour.")


# --------------------------------------------------------------------------
# Mouse / keyboard
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Does the input we send actually arrive
# --------------------------------------------------------------------------
# Mouse and keyboard events we send can **silently vanish**: the API reports
# success and in fact nothing happens. This is the kind of failure this project
# cares about most—not a crash but a silently wrong result, and the person who
# issued the command is not at the computer, so they only see "clicked
# (500, 300)" and wonder why nothing responded.
#
# The two causes need two kinds of detection, and the library has both:
#
# * **Workstation locked / UAC secure desktop**: `input_desktop_available()`
#   asks about the input desktop, which is free and has no side effects, so every
#   input primitive asks before sending (the library caches it for two seconds).
# * **Something is filtering injected input** (this happens when the foreground
#   is a game with anti-cheat): `input_reaches_system()` has to **actually send a
#   key** to test, so it is only used by diagnostic commands, not put into every
#   input primitive—sending a key before every click would be worse than the
#   problem itself. Measured locally: in that situation `OpenInputDesktop`
#   returns normally and the integrity level matches ours (both Medium); only
#   actually sending a key reveals it.
def input_desktop_available() -> bool:
    """Can mouse and keyboard events be sent right now (workstation not locked,
    not on the secure desktop)."""
    try:
        return bool(load_ac().input_desktop_available())
    except Exception as error:  # pylint: disable=broad-except
        # If it cannot be determined, treat it as available: this exists only to
        # give a better error message and must not end up blocking the operation.
        print(f"[gui] input desktop probe failed: {error!r}", file=sys.stderr)
        return True


def input_reaches_system() -> bool:
    """Do the keyboard events we send actually reach the system. **This sends a
    key press** (F13).

    For diagnostics only (`!doctor` / `!screen info`). True may also just mean
    "could not be measured".
    """
    try:
        return bool(load_ac().input_reaches_system())
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] input reach probe failed: {error!r}", file=sys.stderr)
        return True


def _require_input_desktop() -> None:
    """Before sending input, confirm the desktop can receive it. **Release-type
    operations do not call this**—that is the recovery path."""
    if not input_desktop_available():
        raise GuiError("The computer is currently locked, so mouse and keyboard input cannot be sent; "
                       "please unlock it first.")


def mouse_position() -> tuple[int, int]:
    ac = load_ac()
    try:
        pos = ac.get_mouse_position()
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the mouse position.") from error
    if not pos:
        raise GuiError("Failed to read the mouse position.")
    return int(pos[0]), int(pos[1])


def mouse_move(x: int, y: int) -> None:
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.set_mouse_position(x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to move the mouse.") from error


def mouse_click(button: str, x: int | None = None, y: int | None = None,
                *, times: int = 1, interval: float = 0.06) -> None:
    """Click; `times=2` is a double-click.

    A double-click is deliberately "click the same coordinate twice + a short
    interval" rather than a low-level double-click API—je_auto_control has no
    double-click primitive, and Windows decides a double-click only from the time
    gap and the displacement between two clicks, so clicking twice is enough. The
    interval is 60ms, far below the default 500ms threshold.
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        for index in range(max(1, times)):
            if index:
                time.sleep(interval)
            if x is None or y is None:
                ac.click_mouse(button)
            else:
                ac.click_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Mouse click failed.") from error


def mouse_drag(x1: int, y1: int, x2: int, y2: int, button: str = "mouse_left",
               *, steps: int = 24, settle: float = 0.08) -> None:
    """Press at the start point, drag to the end point, then release.

    The movement in between is deliberately split into many slow steps: many
    applications (File Explorer, drawing software, games) detect a drag from
    mouse-move events, and teleporting straight to the end point is taken as
    "press then release" rather than a drag.
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.set_mouse_position(x1, y1)
        time.sleep(settle)
        ac.press_mouse(button, x1, y1)
        time.sleep(settle)
        for index in range(1, steps + 1):
            ac.set_mouse_position(
                int(x1 + (x2 - x1) * index / steps),
                int(y1 + (y2 - y1) * index / steps),
            )
            time.sleep(0.012)
        time.sleep(settle)
        ac.release_mouse(button, x2, y2)
    except Exception as error:  # pylint: disable=broad-except
        # A failure midway through a drag leaves the mouse button stuck down,
        # which effectively locks the whole desktop. Make a best effort to
        # release it here, and do not propagate a failure of that either (the
        # original error matters more).
        try:
            ac.release_mouse(button, x2, y2)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        raise GuiError("Mouse drag failed.") from error


def mouse_scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    """Scroll wheel. Positive scrolls up, negative scrolls down (matching most
    APIs' convention).

    Note the low level clamps x/y to the **primary screen**, so to scroll at a
    point on a secondary monitor, first `mouse move` there and then scroll without
    coordinates.
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.mouse_scroll(int(amount))
        else:
            ac.mouse_scroll(int(amount), x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Scrolling failed.") from error


# --------------------------------------------------------------------------
# Typing
# --------------------------------------------------------------------------
def type_text(text: str) -> None:
    """Send text as keyboard input to the currently focused window.

    Handed straight to the low-level `write()`. It chooses a path per character:
    characters in the key-name table are sent as virtual keys, those that are not
    (punctuation, Chinese/Japanese, emoji) as Unicode character events, and
    newline and Tab as real Enter / Tab keys. **This part is deliberately not
    re-implemented in this project**—the same thing should have only one
    implementation, and it belongs to the desktop-automation library, not to this
    bot.
    """
    _require_input_desktop()
    if not text:
        return
    ac = load_ac()
    try:
        ac.write(text)
    except Exception as error:  # pylint: disable=broad-except
        # On failure, run one round of releases, for exactly the same reason as
        # `press_hotkey`: the low-level `type_keyboard` is "press → release" with
        # **no finally** in between, so if the release step fails after the press,
        # that key stays pressed. This path also bypasses the three layers of
        # safety this module claims (the `_HELD_INPUTS` registry /
        # `release_all_inputs` / the timed auto-release only hang off `key_down` /
        # `key_up`), so `/input key status` would say nothing is held and
        # `/input key clear` could not release it either.
        #
        # This side is harder to recover from on its own than a key combination:
        # what gets stuck is an **ordinary character key**, which Windows keeps
        # auto-repeating, so that character gets typed on screen endlessly while
        # the person who issued the command is not at the computer.
        # (The combination path gets a modifier stuck; this
        # `write(is_shift=False)` does not touch shift.)
        stuck = _undo_write_press(ac, text)
        raise GuiError(
            "Keyboard input failed (this text contains characters that cannot be typed directly); "
            f"the {len(stuck)} key(s) this text would use have been released again, "
            "so the keyboard will not get stuck."
        ) from error


# `write()` turns newline / Tab / backspace into real key presses rather than
# typing that control character. This mapping must match the low-level
# `WRITE_CONTROL_KEYS`; use that one when it can be read, and only fall back to
# this copy when it cannot (it is an internal constant, not guaranteed to stay,
# but even if it drifts, the worst case is releasing one key too few).
_WRITE_CONTROL_KEYS_FALLBACK = {"\n": "return", "\r": "return",
                                "\t": "tab", "\x08": "back"}

# The most distinct keys to reclaim for one piece of text. Ordinary ASCII text has
# far fewer distinct characters than this; the cap only stops a pathological long
# string from turning the "remedy" itself into hundreds of input events.
_UNDO_WRITE_MAX_KEYS = 64


def _write_control_keys(ac: Any) -> dict:
    """The low level's "control character → key name" mapping, or the fallback
    copy when it cannot be read. Never raises."""
    for holder in (ac, getattr(ac, "wrapper", None)):
        table = getattr(holder, "WRITE_CONTROL_KEYS", None)
        if isinstance(table, dict) and table:
            return table
    try:
        from je_auto_control.wrapper.auto_control_keyboard import (  # type: ignore
            WRITE_CONTROL_KEYS,
        )
        if isinstance(WRITE_CONTROL_KEYS, dict) and WRITE_CONTROL_KEYS:
            return WRITE_CONTROL_KEYS
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    return _WRITE_CONTROL_KEYS_FALLBACK


def keys_a_write_could_press(ac: Any, text: str) -> list:
    """The keys that **may get pressed** when this text is handed to `write()`
    (deduplicated, capped).

    Pure table lookup, sending no input events, so it can be tested. The order
    follows each key's first appearance in the text, so failure messages and logs
    read predictably.

    It mirrors the branches of the low-level `write()`: control character →
    `WRITE_CONTROL_KEYS`; a character in the table → its key code; one not in the
    table → a Unicode event (**presses no key**, so it is not listed); whitespace
    that still does not fit → `space`.
    """
    control = _write_control_keys(ac)
    table = getattr(ac, "keyboard_keys_table", None)
    if not isinstance(table, dict):
        table = {}
    out: list = []
    seen = set()

    def _add(key):
        if key is None or key in seen:
            return
        seen.add(key)
        out.append(key)

    for char in text or "":
        if len(out) >= _UNDO_WRITE_MAX_KEYS:
            break
        mapped = control.get(char)
        if mapped is not None and mapped in table:
            _add(mapped)
        elif char in table:
            _add(table[char])
        elif char.isspace():
            # The Unicode path presses no key, so only "not in the table, but
            # whitespace" goes through space.
            _add("space")
    return out


def _undo_write_press(ac: Any, text: str) -> list:
    """When `write()` fails partway, run one round of releases for the keys this
    text may still be holding down. Returns the ones actually released.

    Same stance as `_undo_hotkey_press`: **release only the keys this one call may
    itself have pressed**, with no "release every modifier while we are at it"
    sweep—the user may be standing at the keyboard holding ctrl. Releasing a key
    that is not held is safe (the OS does nothing for a key already up; `key_up`'s
    docstring takes the same stance), so there is no need to know how many
    characters were typed before the failure.
    """
    released: list = []
    try:
        candidates = keys_a_write_could_press(ac, text)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] write undo: cannot resolve keys: {error!r}",
              file=sys.stderr)
        return released
    for key in candidates:
        try:
            ac.release_keyboard_key(_library_key(key))
            released.append(key)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] write undo release {key!r} failed: {error!r}",
                  file=sys.stderr)
    return released


def _undo_hotkey_press(ac: Any, tokens: list[str]) -> list[str]:
    """When a key combination fails partway, run one round of releases in reverse
    for the keys that may still be held. Returns which ones were actually
    released.

    Release **only the keys we ourselves asked to press**, with no "release every
    modifier while we are at it" sweep—the user may be at the keyboard holding
    ctrl, and releasing it for them is another kind of silent error.

    Releasing a key that is not held is safe: the low level sends a release event,
    and the OS does nothing for a key that is already up (`key_up`'s docstring
    takes the same stance). So there is no need to know how far the presses got
    before the failure; releasing them all once in reverse is enough.
    """
    released: list[str] = []
    for token in reversed(tokens):
        try:
            ac.release_keyboard_key(_library_key(token))
            released.append(token)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] hotkey undo release {token!r} failed: {error!r}",
                  file=sys.stderr)
    return released


def press_hotkey(tokens: list[str]) -> None:
    """Send a key combination (press in order, then release in reverse).

    **On failure, always run one round of reverse releases.** The low-level
    `je_auto_control.hotkey` is "for press → for release" with **no finally** in
    between. Measured on this machine on 2026-08-30 (with the low-level
    press/release swapped for fake recorders, never touching the real desktop): a
    three-key combination raising on the second key press produced a call
    sequence of only `press ctrl` / `press shift`, **without a single
    release**—ctrl and shift were simply left pressed.

    This path bypasses the three layers of safety this module claims, because
    those three layers (the `_HELD_INPUTS` registry, `release_all_inputs`, the
    timed auto-release) only hang off `key_down` / `key_up`: a key combination is
    never registered from start to finish, so `/input key status` would say
    nothing is held, `/input key clear` could not release it, and there is no
    timeout timer at all. The result is the whole computer acting broken (with alt
    stuck, every key press becomes a menu shortcut) while the person who issued
    the command is not at the computer.

    Nor does this only happen with a mistyped key name: key names go through
    `parse_key_name` first, and the real trigger is the OS layer failing between
    two presses (a UAC screen cutting in, input blocked by the secure desktop).
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.hotkey(_library_keys(tokens))
    except Exception as error:  # pylint: disable=broad-except
        _undo_hotkey_press(ac, tokens)
        raise GuiError("Sending the key combination failed (possibly a key name that does not exist); "
                       "the keys have been released again, so the keyboard will not get stuck.") from error


# --------------------------------------------------------------------------
# Hold / release (modifier + click, games, long press)
# --------------------------------------------------------------------------
# `press_hotkey` is "press and release at once", which cannot do "hold W and walk
# for three seconds", "hold ctrl and click five files" or "hold the left button,
# draw a line, then release". This splits down / up apart.
#
# The price is that **state stays on the host**: one key left unreleased makes the
# whole computer act broken (with alt stuck, every key press becomes a menu
# shortcut), and the person who issued the command is not at the computer and
# cannot see that it is stuck. So there are three layers of safety, and none can
# be dropped:
#
# 1. Every hold is registered in `_HELD_INPUTS`, and `held_inputs()` can look it
#    up at any time;
# 2. `release_all_inputs()` releases everything at once (called by `!key clear`
#    and at the end of a macro);
# 3. The caller additionally hangs a timed auto-release on it
#    (`release_input_if_stale`)—pressing something and forgetting it is the norm.
#
# All three layers rest on the same invariant: **a registry entry only leaves
# `_HELD_INPUTS` after "the release was actually sent"** (`_release_keys`). An
# entry whose release failed stays, with its press time unchanged, so
# `/input key status` can see it and the next `/input key clear` / `/host panic` /
# macro wrap-up / timeout retry will all release it again.
# Before 2026-09-21 it was "removed whether or not it succeeded", so a key whose
# release failed vanished from all three layers at once—exactly the "stuck but
# invisible" this comment exists to prevent.
_HELD_INPUTS: dict[tuple[str, str], float] = {}

# When nobody releases it, how long before it is released automatically.
# Scheduling is the caller's job (this module has no event loop).
INPUT_HOLD_MAX_SEC = 300.0

# How many more times, and how far apart, to retry when the timed auto-release
# **fails** (scheduled by the caller; see the bot's `_auto_release_input` for the
# reasons). The main cause of a failed release is the OS not accepting synthetic
# input at that moment (the library raises when `SendInput` returns 0: input
# blocked by another thread, the secure desktop), a state that passes on its own,
# so it is worth retrying; but without a cap, a key that can never be released
# would become a background task that lives forever and writes a log line every so
# often. If it still cannot be released after the retries, it stays in
# `/input key status` for a human to deal with.
INPUT_RELEASE_RETRIES = 3
INPUT_RELEASE_RETRY_SEC = 60.0


def parse_key_name(raw: str) -> str:
    """Validate a key name. Raise `GuiError` if it is invalid, so arbitrary
    strings never reach the low-level key-name table.

    When the backend cannot load, only the literal check is done and nothing is
    blocked—this function is also called by the macro's **save-time validation**,
    and saving a macro in an environment with no desktop session (tests, CI)
    should not fail for lack of a backend.

    Names in `_EXTRA_KEY_CODES` and `_LIBRARY_NAME_OVERRIDES` need not be checked
    against the low-level table (the former are not in that table at all, the
    latter have wrong values there); what is returned is still the **name**;
    converting it to an integer is what `_library_key` does at the moment of
    sending.
    """
    key = (raw or "").strip().lower()
    if not key or not HOTKEY_TOKEN_RE.match(key):
        raise GuiError("A key name may only contain letters, digits and underscores.")
    key = KEY_ALIASES.get(key, key)
    if key in _EXTRA_KEY_CODES or key in _LIBRARY_NAME_OVERRIDES:
        return key
    try:
        table = getattr(load_ac(), "keyboard_keys_table", None)
    except GuiError:
        return key
    if isinstance(table, dict) and key not in table:
        raise GuiError("Unknown key name.")
    return key


def key_down(name: str) -> str:
    """Hold a key down."""
    key = parse_key_name(name)
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.press_keyboard_key(_library_key(key))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to press the key.") from error
    _HELD_INPUTS[("key", key)] = time.monotonic()
    return key


def key_up(name: str) -> str:
    """Release a key. **The release event is sent even if it is not held**—the
    point is to free a stuck key, not to insist on consistent state (after a bot
    restart `_HELD_INPUTS` is empty, but the key is still pressed)."""
    key = parse_key_name(name)
    ac = load_ac()
    try:
        ac.release_keyboard_key(_library_key(key))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to release the key.") from error
    _HELD_INPUTS.pop(("key", key), None)
    return key


def mouse_button_down(button: str, x: int | None = None,
                      y: int | None = None) -> None:
    _require_input_desktop()
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.press_mouse(button)
        else:
            ac.press_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to press the mouse button.") from error
    _HELD_INPUTS[("mouse", button)] = time.monotonic()


def mouse_button_up(button: str, x: int | None = None,
                    y: int | None = None) -> None:
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.release_mouse(button)
        else:
            ac.release_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to release the mouse button.") from error
    _HELD_INPUTS.pop(("mouse", button), None)


def held_inputs() -> list[tuple[str, str, float]]:
    """Keys / mouse buttons currently held down: `[(kind, name, seconds held), …]`."""
    now = time.monotonic()
    return sorted(
        ((kind, name, now - since) for (kind, name), since in _HELD_INPUTS.items()),
        key=lambda row: -row[2],
    )


def input_pressed_at(kind: str, name: str) -> float | None:
    """When a key was pressed; None if it is not held. For the caller to schedule
    the timed auto-release."""
    return _HELD_INPUTS.get((kind, name))


def held_snapshot() -> set[tuple[str, str]]:
    """The set currently held. For "release only the ones we pressed ourselves"
    (see `release_added_since`)."""
    return set(_HELD_INPUTS)


def release_all_inputs() -> list[str]:
    """Release everything, returning the names released. A single failure does not
    stop it—the point is to unlock the desktop as far as possible.

    The ones that could not be released **are not in the return value and do not
    leave the registry**; to learn how many could not be released, use
    `release_all_inputs_report`.
    """
    return _release_keys(list(_HELD_INPUTS))[0]


def release_all_inputs_report() -> tuple[list[str], list[str]]:
    """Same as `release_all_inputs`, additionally returning the ones that could not
    be released: `(released, stuck)`.

    For callers that **report to a person** (`/input key clear`, `/host panic`):
    replying only "released N" would silently leave out the one that could not be
    released, and the person who issued the command is exactly the one who cannot
    see the keyboard.
    """
    return _release_keys(list(_HELD_INPUTS))


def release_added_since(snapshot: set[tuple[str, str]]) -> list[str]:
    """Release only what was pressed after the snapshot.

    A macro uses this rather than `release_all_inputs` when it finishes: the user
    may have done `!key down ctrl` themselves before running the macro, and the
    macro should not release it for them. The ones that cannot be released also
    stay in the registry.
    """
    return _release_keys([k for k in _HELD_INPUTS if k not in snapshot])[0]


def release_input_if_stale(kind: str, name: str, pressed_at: float) -> bool:
    """Automatically release a hold that exceeded the limit; return whether it
    was actually released.

    Comparing `pressed_at` is deliberate: the user may release and then press the
    same key again, and the old timeout timer must not release the new press. A
    different timestamp means it is not the same hold.

    When the release **fails** it returns False, and that entry's press time is
    left untouched—so the same timer calling again with the same `pressed_at` is a
    retry (the caller uses this to tell "not the same hold" from "release failed":
    after the former, `input_pressed_at` no longer equals `pressed_at`; after the
    latter it still does).
    """
    if _HELD_INPUTS.get((kind, name)) != pressed_at:
        return False
    return bool(_release_keys([(kind, name)])[0])


def _release_keys(keys: list[tuple[str, str]]
                  ) -> tuple[list[str], list[str]]:
    """Release one by one, returning `(names released, names that could not be
    released)`.

    **Only successfully released ones are removed from `_HELD_INPUTS`.** When a
    release raises, that key is very likely still pressed, and removing its entry
    then would make it vanish from all three layers of safety at once:
    `/input key status` says no key is held, `/input key clear`'s count is one
    short, and the timeout timer can never find it again. The entry that stays
    keeps its press time, so the timeout timer's comparison still holds.

    When the backend cannot load, **nothing is removed** and everything counts as
    not released. In a real run that branch is actually unreachable: an entry is
    only written **after** a successful press, and a successful press means
    `load_ac()` already succeeded, which caches the module in `_AC` and never goes
    back to None. It is deliberately not written as `clear()` (it used to be): the
    only way to reach it is someone resetting the cache, and at that point these
    keys may still be pressed, so clearing them is the same "stuck but invisible".
    """
    try:
        ac = load_ac()
    except GuiError:
        return [], [name for _kind, name in keys]
    released: list[str] = []
    failed: list[str] = []
    for kind, name in keys:
        try:
            if kind == "key":
                ac.release_keyboard_key(_library_key(name))
            else:
                ac.release_mouse(name)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] release {kind} {name!r} failed: {error!r}", file=sys.stderr)
            failed.append(name)
            continue
        released.append(name)
        _HELD_INPUTS.pop((kind, name), None)
    return released, failed


def paste_text(text: str) -> None:
    """Put text on the clipboard and then send `ctrl+v`.

    `type_text` uses per-character keyboard simulation, which cannot type
    Chinese/Japanese or most non-ASCII characters; the only reliable way to enter
    such content is clipboard + paste.
    """
    set_clipboard(text)
    time.sleep(0.05)
    # This must go through the alias layer: the low-level key-name table has
    # **no** `ctrl` (only `control`), and a key combination is handed to the
    # library's table lookup as-is. This used to hardcode `["ctrl", "v"]`, so every
    # paste failed at the library's lookup step (measured against the real table
    # on 2026-09-21: `_resolve_keycode("ctrl")` raises
    # `AutoControlCantFindKeyException`); the fake backend used in tests accepts
    # any name, so it never showed.
    press_hotkey(parse_hotkey_tokens("ctrl+v"))


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------
def get_clipboard() -> str:
    ac = load_ac()
    try:
        return ac.get_clipboard() or ""
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the clipboard.") from error


def set_clipboard(text: str) -> None:
    ac = load_ac()
    try:
        ac.set_clipboard(text)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to write to the clipboard.") from error


def get_clipboard_image(dest: Path) -> bool:
    """If the clipboard holds an image, save it to `dest` and return True;
    return False if it is not an image.

    "Take a screenshot onto the clipboard" is a very common step, but
    `get_clipboard()` only sees text, so to it an image is as good as
    empty—the user would think the clipboard is empty.

    The library returns PNG bytes, so `dest` always uses `.png`.
    """
    try:
        payload = load_ac().get_clipboard_image()
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the clipboard image.") from error
    if not payload:
        return False        # empty, or the clipboard holds "files" rather than the image itself
    try:
        dest.write_bytes(payload)
    except OSError as error:
        raise GuiError("Failed to save the clipboard image.") from error
    return True


def set_clipboard_image(source: Path) -> None:
    """Put an image on the clipboard so it can then be pasted directly into any
    program.

    The library's `set_clipboard_image` accepts both PNG bytes and a file path;
    this passes a path.
    """
    try:
        load_ac().set_clipboard_image(str(source))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to put the image on the clipboard.") from error


def clipboard_file_list() -> list[str]:
    """The list of file paths on the clipboard after pressing "Copy" in File
    Explorer.

    The third kind of clipboard content. Text cannot see it and neither can the
    image path—copying a batch of files and then typing `!clip` would report
    "empty", exactly the same kind of misdirection the image case once was.

    **It returns full paths on the host**; the caller may only use it to count
    items and read extensions, and must never send the list out whole
    (Secrecy Layer 1).
    """
    try:
        return list(load_ac().get_clipboard_files() or [])
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the clipboard file list.") from error


def clipboard_kinds() -> dict[str, Any]:
    """**Which kinds** of content the clipboard currently holds:
    `{categories, has_text, has_image, has_files}`.

    The format list (`formats`) is deliberately not passed on: those are Win32
    format names, meaningless to the user, and some programs name their custom
    formats with strings containing paths or internal product code names.
    """
    try:
        summary = load_ac().clipboard_formats() or {}
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to read the clipboard formats.") from error
    return {
        "categories": list(summary.get("categories") or []),
        "has_text": bool(summary.get("has_text")),
        "has_image": bool(summary.get("has_image")),
        "has_files": bool(summary.get("has_files")),
    }


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
def list_windows() -> list[tuple[int, str]]:
    """All visible, titled top-level windows, in z-order (frontmost first)."""
    api = _window_api()
    try:
        return api.list_windows(titled_only=True)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to enumerate windows.") from error


def match_windows(needle: str) -> list[tuple[int, str]]:
    """Windows whose title contains `needle` (case-insensitive)."""
    key = (needle or "").strip().lower()
    if not key:
        raise GuiError("Give part of the window title.")
    return [(hwnd, title) for hwnd, title in list_windows() if key in title.lower()]


# `!win <action>` → the cmd value for Win32 `ShowWindow`. `close` is not here; it
# goes through WM_CLOSE.
WINDOW_SHOW_ACTIONS = {
    "min": 6,        # SW_MINIMIZE
    "minimize": 6,
    "max": 3,        # SW_MAXIMIZE
    "maximize": 3,
    "restore": 9,    # SW_RESTORE
    "hide": 0,       # SW_HIDE
    "show": 5,       # SW_SHOW
}


def window_show(needle: str, action: str) -> tuple[int, str, int]:
    """Apply min / max / restore / hide / show to the first matching window.

    Returns `(hwnd, title, match count)`; the caller should only report the match
    count to the user, and **the title must not be sent out** (IDEs / editors
    conventionally put an absolute host path in the title bar).
    """
    api = _window_api()
    cmd_show = WINDOW_SHOW_ACTIONS.get((action or "").strip().lower())
    if cmd_show is None:
        raise GuiError("The window action must be `min` / `max` / `restore` / `show` / `hide` / `close`.")
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    try:
        api.show_window_by_title(needle, cmd_show)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to change the window state.") from error
    return hwnd, title, len(matched)


def window_close(needle: str) -> tuple[int, str, int]:
    """Send `WM_CLOSE` to the first matching window (the same as clicking the
    close button in the top-right corner).

    `TerminateProcess` is deliberately not used: `WM_CLOSE` lets the program run
    its own wrap-up (save prompts, writing settings back), and a user who wants to
    kill the process outright already has `!kill`.
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    try:
        api.close_window_by_title(needle)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to close the window.") from error
    return hwnd, title, len(matched)


def window_focus(needle: str) -> tuple[int, str, int]:
    """Restore it (if minimised) and bring it to the foreground."""
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    try:
        # The library's focus_window restores a minimised window first and then
        # brings it to the foreground.
        api.focus_window(needle)
    except Exception as error:  # pylint: disable=broad-except
        # SetForegroundWindow is access-denied under the alt-tab lock /
        # foreground lock.
        raise GuiError("The window was found, but the system does not allow bringing it to the foreground.") from error
    return hwnd, title, len(matched)


def foreground_window() -> tuple[int, str] | None:
    """The current frontmost window `(hwnd, title)`; None if unavailable."""
    try:
        return _window_api().foreground_window()
    except Exception:  # pylint: disable=broad-except
        return None


def window_rect(needle: str) -> tuple[int, str, tuple[int, int, int, int], int]:
    """The first matching window's `(hwnd, title, (x, y, width, height), match
    count)`.

    The coordinates are in **the same space** as the mouse (this process is
    DPI-unaware, and `GetWindowRect` returns virtualised logical coordinates), so
    whatever is measured can go straight into `!click`.

    This primitive is the precondition for "reliably clicking inside a given
    window": without it, the user can only count pixels on a screenshot, and every
    coordinate goes wrong as soon as the window moves.
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    rect = api.window_rect(needle)
    if rect is None:
        raise GuiError("Failed to read the window position.")
    left, top, right, bottom = rect
    return hwnd, title, (left, top, right - left, bottom - top), len(matched)


def window_move(needle: str, x: int, y: int, width: int | None = None,
                height: int | None = None) -> tuple[int, str, int]:
    """Move the first matching window to `(x, y)`, optionally also resizing it to
    `width × height`.

    The low level uses `MoveWindow`, which only changes position and size, without
    touching the z-order or stealing focus—raising the window to the top and
    stealing focus when all you want is to position it would interrupt what the
    user is doing, and send the following keyboard actions to the wrong place.
    When width and height are omitted, the library reads the current size and keeps
    it, so the window is never shrunk to 0.
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    try:
        moved = api.move_window_by_title(needle, x, y, width, height)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("Failed to move the window.") from error
    if not moved:
        raise GuiError("Failed to move the window.")
    return hwnd, title, len(matched)


def wait_window(needle: str, timeout: float, poll: float = 0.5, *,
                should_abort: Callable[[], bool] | None = None
                ) -> tuple[int, str]:
    """Poll until the window appears. Raises `GuiError` on timeout and
    `GuiAborted` when aborted."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matched = match_windows(needle)
        if matched:
            return matched[0]
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and no matching window appeared.")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# Window layouts (save / restore / snap / grid)
# --------------------------------------------------------------------------
# In remote operation, what takes the most time is often not "where to click" but
# rearranging the screen into something readable. The implementation is entirely
# in the library (`window_capture`'s `save_window_layout` / `restore_window_layout`
# / `snap_window` / `arrange_grid`); this only does name validation, the on-disk
# location and the error messages.
LAYOUT_DIR = PROJECT_ROOT / "window_layouts"
LAYOUT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def layout_path(name: str) -> Path:
    """The layout file path. Names are limited to letters, digits / `_` / `-`,
    blocking path traversal such as `../`."""
    key = (name or "").strip()
    if not LAYOUT_NAME_RE.match(key):
        raise GuiError("A layout name may only contain letters, digits, underscores and hyphens, "
                       "length 1..40.")
    return LAYOUT_DIR / f"{key}.json"


def save_window_layout(name: str) -> int:
    """Save the position and size of every titled window right now; return how
    many were saved."""
    path = layout_path(name)
    ac = load_ac()
    try:
        LAYOUT_DIR.mkdir(parents=True, exist_ok=True)
        entries = ac.save_window_layout(str(path))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] save_window_layout failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to save the window layout.") from error
    return len(entries or [])


def restore_window_layout(name: str) -> int:
    """Put windows back where they were when saved; return how many were actually
    moved.

    Windows closed after saving are not reopened—the library skips titles it
    cannot find, so a return value smaller than at save time is normal, and the
    caller must say so plainly rather than report success.
    """
    path = layout_path(name)
    if not path.is_file():
        raise GuiError("No layout with that name was found.")
    ac = load_ac()
    try:
        return int(ac.restore_window_layout(str(path)))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] restore_window_layout failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to restore the window layout.") from error


def list_window_layouts() -> list[tuple[str, int, float]]:
    """`[(name, window count, modified time), …]`, newest first."""
    out: list[tuple[str, int, float]] = []
    try:
        paths = sorted(LAYOUT_DIR.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, list) else 0
            out.append((path.stem, count, path.stat().st_mtime))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def delete_window_layout(name: str) -> None:
    path = layout_path(name)
    if not path.is_file():
        raise GuiError("No layout with that name was found.")
    try:
        path.unlink()
    except OSError as error:
        raise GuiError("Failed to delete the layout.") from error


# Position names must match the library's `_snap_rect`—measured, `maximize` is not
# a legal value for it (it is called `max`), and sending it only raises ValueError,
# which is then folded into a generic failure. This list is exactly the nine it
# supports, with the memorable `maximize` taken in as an alias for `max`.
SNAP_POSITIONS = ("left", "right", "top", "bottom", "top-left", "top-right",
                  "bottom-left", "bottom-right", "max")
SNAP_ALIASES = {"maximize": "max", "full": "max", "tl": "top-left",
                "tr": "top-right", "bl": "bottom-left", "br": "bottom-right"}


def snap_window(needle: str, position: str) -> tuple[int, str]:
    """Snap the first matching window to one half / one corner of the screen.
    Returns `(hwnd, title)`."""
    key = (position or "left").strip().lower()
    key = SNAP_ALIASES.get(key, key)
    if key not in SNAP_POSITIONS:
        raise GuiError(
            "The position must be `left` / `right` / `top` / `bottom` / `top-left` / "
            "`top-right` / `bottom-left` / `bottom-right` / `max`.")
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        ok = ac.snap_window(title, key)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] snap_window failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to snap the window.") from error
    if not ok:
        raise GuiError("Failed to snap the window.")
    return hwnd, title


def grid_windows(needles: list[str], *, gap: int = 0) -> int:
    """Arrange several windows in a grid; return how many were actually
    arranged."""
    titles: list[str] = []
    for needle in needles:
        matched = match_windows(needle)
        if not matched:
            raise GuiError(f"No window matching \"{needle}\" was found.")
        titles.append(matched[0][1])
    ac = load_ac()
    try:
        return int(ac.arrange_grid(titles, gap=max(0, int(gap))))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] arrange_grid failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to arrange the windows.") from error


# --------------------------------------------------------------------------
# Background window input (without stealing focus)
# --------------------------------------------------------------------------
# The ordinary `!click` / `!key` use system-level input, which always acts on the
# **foreground** window, so remote operation interrupts whatever the user is doing
# at that moment. This path instead uses `PostMessage` to deliver messages
# straight to the target window.
#
# **The cost must be stated clearly**: a posted message is not real input. Many
# programs (games, programs that require raw input, programs that check the
# foreground state themselves) simply ignore it, and a successful `PostMessage`
# only means "the message was queued", not that the other side handled it—this is
# exactly the "silent success" shape this project cares about most, so the caller
# **must** tell the user this path is best-effort and to switch to foreground
# operation if nothing happens.
#
# It uses the library's `post_key_to_window` / `post_click_to_window`, **not** the
# older `send_key_event_to_window`: the latter posts to the top-level window, while
# keyboard messages go to **the focused child control**. Measured (with Character
# Map): posting to the frame did nothing, and only posting to the focused control
# got the character in, so the old path is effectively a no-op for any program
# with child controls—yet another kind of silent success.
def send_key_to_window(needle: str, key: str) -> tuple[int, str, str]:
    """Post one key stroke (press + release) to the matching window, without
    stealing focus.

    Returns `(hwnd, title, normalised key name)`.
    """
    name = parse_key_name(key)
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        posted = ac.post_key_to_window(title, _library_key(name))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] post_key_to_window failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to send the background key press.") from error
    if not posted:
        raise GuiError("Failed to send the background key press.")
    return hwnd, title, name


def send_click_to_window(needle: str, button: str, x: int, y: int
                         ) -> tuple[int, str]:
    """Post one click to the matching window, without stealing focus. The
    coordinates are **relative to the window**."""
    key = parse_button(button)
    matched = match_windows(needle)
    if not matched:
        raise GuiError("No matching window was found.")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        posted = ac.post_click_to_window(title, key, int(x), int(y))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] post_click_to_window failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to send the background click.") from error
    if not posted:
        raise GuiError("Failed to send the background click.")
    return hwnd, title


# --------------------------------------------------------------------------
# Wait conditions: port / process / clipboard
# --------------------------------------------------------------------------
# All three delegate to the library's `smart_waits`: it owns both the probing and
# the polling, and this only folds the result of "wait a short while" into a
# bool, so the bot's watch loop can ask at its own pace.
def parse_host_port(raw: str) -> tuple[str, int]:
    """`8080` / `127.0.0.1:8080` / `example.com:443` → `(host, port)`."""
    text = (raw or "").strip()
    if not text:
        raise GuiError("A port is required, e.g. `8080` or `127.0.0.1:8080`.")
    host = "127.0.0.1"
    port_text = text
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        host = host.strip() or "127.0.0.1"
    try:
        port = int(port_text)
    except ValueError as error:
        raise GuiError("The port must be an integer.") from error
    if not 0 < port <= 65535:
        raise GuiError("The port must be between 1 and 65535.")
    return host, port


def port_open(host: str, port: int, *, timeout: float = 1.5) -> bool:
    """Whether that port accepts connections right now."""
    ac = load_ac()
    try:
        outcome = ac.wait_until_port(
            host, int(port), timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2, connect_timeout_s=1.0)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_port failed: {error!r}", file=sys.stderr)
        return False
    return bool(outcome.succeeded)


def process_running(name: str, *, timeout: float = 1.0) -> bool:
    """Whether a process with this name is running (the matching is done by the
    library, as a case-insensitive substring match)."""
    target = (name or "").strip()
    if not target:
        raise GuiError("A process name is required, e.g. `notepad.exe`.")
    ac = load_ac()
    try:
        outcome = ac.wait_until_process(
            target, present=True, timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_process failed: {error!r}", file=sys.stderr)
        return False
    return bool(outcome.succeeded)


def clipboard_changed(baseline: str, *, contains: str = "",
                      timeout: float = 1.0) -> bool:
    """Whether the clipboard has changed; given `contains`, it instead checks
    "whether the content contains that text"."""
    ac = load_ac()
    try:
        outcome = ac.wait_until_clipboard_changes(
            baseline=baseline, target=contains or None,
            contains=bool(contains), timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_clipboard_changes failed: {error!r}",
              file=sys.stderr)
        return False
    return bool(outcome.succeeded)


# --------------------------------------------------------------------------
# Text recognition (OCR)
# --------------------------------------------------------------------------
# **Recognition and location themselves are handed to the desktop-automation
# library** (`find_text_matches` / `read_text_in_region` / `group_lines`). This
# module once had its own implementation, because back then the coordinates the
# library returned were wrong on this host, and wrong in a way that did not show
# (the click landed slightly off, looking like "sometimes fails" rather than
# "broken"): a whole-desktop screenshot is in **physical pixels** (3840×1244
# locally) while mouse coordinates are logical pixels (3456×1244, secondary
# monitor at 125% scaling), and in-image coordinates were returned directly as
# screen coordinates, ignoring the virtual-desktop origin (locally y starts at
# −164). Both defects, together with "only single recognised words are matched,
# so a target spanning words is never found", have been fixed on the library
# side, so this project no longer keeps a second implementation.
#
# What remains in this section is **local environment policy**, which never
# belonged to the library:
#
# * the location of the engine executable (the installer does not add itself to
#   PATH);
# * language data lives in the project's `ocr_tessdata/` (`TESSDATA_PREFIX`
#   points there) rather than the engine's install directory—that directory is
#   under `Program Files` and cannot be written without administrator rights, and
#   asking the user to elevate just to add one language file is unreasonable;
# * picking the recognition language automatically from the target string (see
#   `ocr_lang_for`).
LOCAL_TESSDATA = PROJECT_ROOT / "ocr_tessdata"

# By default the installer does **not** add itself to PATH (measured locally:
# after installing, `where tesseract` still finds nothing). Checking only PATH
# would leave this feature silently unusable on most hosts, so the conventional
# install locations are added.
TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# CJK characters; used to decide the default recognition language.
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")

_OCR: Any = None
_OCR_TRIED = False
_OCR_CONFIGURED = False
_OCR_REASON = "Text recognition is disabled: the recognition engine is unavailable."
_OCR_PROBED_CMD: str | None = None


def tesseract_cmd() -> str | None:
    """The absolute path of the recognition engine executable; None if not found.

    Order: environment-variable override → PATH → conventional install locations.
    """
    override = (os.environ.get("TESSERACT_CMD") or "").strip()
    if override and Path(override).exists():
        return override
    import shutil
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _configure_ocr() -> None:
    """Tell the desktop-automation library the local engine path and
    language-data location (only once, after it succeeds).

    These two are **local environment policy**, not recognition logic: the engine
    installer does not add itself to PATH, and the language data lives in the
    project because the engine's install directory needs administrator rights.
    Recognition itself is handed to the library; this module no longer runs its
    own.

    ⚠️ **Do not record "already configured" when no executable was found.** This
    flag used to be set unconditionally at the top of the function, so on a host
    without the engine installed yet, the first call recorded "configured" while
    configuring nothing; once the user later installed the engine (the bot is a
    long-running process under a supervisor and does not restart by itself), the
    library would **never** get that path—and actual recognition goes through the
    library, not through this module's `_OCR`. The failure is quiet:
    `ocr_status()` says it is available, while every recognition command returns
    the generic "Text recognition failed.". Measured (2026-09-11): with the engine
    installed late, `set_tesseract_cmd` was called **0** times, versus once in the
    clean-start control.
    """
    global _OCR_CONFIGURED  # pylint: disable=global-statement
    if _OCR_CONFIGURED:
        return
    if LOCAL_TESSDATA.is_dir() and any(LOCAL_TESSDATA.glob("*.traineddata")):
        # setdefault: if the user already set it themselves, respect their setting.
        os.environ.setdefault("TESSDATA_PREFIX", str(LOCAL_TESSDATA))
    command = tesseract_cmd()
    if not command:
        return
    try:
        load_ac().set_tesseract_cmd(command)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] set_tesseract_cmd failed: {error!r}", file=sys.stderr)
        return
    _OCR_CONFIGURED = True


def _load_ocr() -> Any:
    """Return the configured `pytesseract` module; raise `GuiError` if it is
    unavailable.

    Only the **language-list query** still needs it (the library's facade has no
    entry point for that). Actual recognition and location go through
    `_ocr_call`, which delegates to the library.

    ⚠️ **The reason for a failure is recorded in `_OCR_REASON`; do not call back
    into `ocr_status()` to ask for that sentence.** This used to read
    `raise GuiError(ocr_status()[1])`, and the third check in `ocr_status()` back
    then was a call to `_load_ocr()`—**the exception's argument is evaluated
    before the raise**, so that `except GuiError` was never reached; what hit first
    was a `RecursionError` (measured at 1000 levels). Along the way it turned the
    sentence "the recognition engine executable cannot run." into dead code that was
    never sent even once. The call direction is now one-way: `ocr_status()`
    (reporting) → `_load_ocr()` (probing), and the probe can state the reason
    itself. `test_bot_helpers.py` has a cross-project AST guard (a `raise`'s
    argument must not lead back to the function containing the raise) watching
    that this edge never reverses—**behaviour tests cannot catch** a regression
    that "reverts just one of the raises", because the re-probe below makes that
    cycle converge on its own at the second level, as verified by a mutation run.

    ⚠️ **A failed result is only cached for as long as "the executable path seen
    at probe time" stays the same.** Recording only `_OCR_TRIED` would, on a host
    without the engine installed yet, pin `_OCR=None` on the first recognition
    command; once the user later installed the engine, this would never retry. And
    that is exactly the path most likely to trigger the recursion above: measured,
    after "no engine installed → one text command issued → engine installed", this
    long-lived process's `ocr_status()` and `_load_ocr()` hit `RecursionError`
    permanently, with no broken install, no architecture mismatch and no wrong
    `TESSERACT_CMD` needed. A successful result is **cached permanently**, and
    returns early before `tesseract_cmd()` (which scans PATH, measured locally at
    4.0 ms)—`wait_text` polls `find_text` every 0.5 seconds, and that path cannot
    afford any extra cost (the early return measured 0.0001 ms per call).

    Remaining gaps (both need a bot restart, and both are better than the original
    `RecursionError`): repairing a broken engine in place at **the same path**, and
    pip-installing that Python package after the process has started.
    """
    global _OCR, _OCR_TRIED, _OCR_REASON, _OCR_PROBED_CMD  # pylint: disable=global-statement
    if _OCR is not None:
        return _OCR
    command = tesseract_cmd()
    if _OCR_TRIED and command == _OCR_PROBED_CMD:
        raise GuiError(_OCR_REASON)
    _OCR_TRIED = True
    _OCR_PROBED_CMD = command
    try:
        import pytesseract  # type: ignore
    except ImportError as error:
        print(f"[gui] pytesseract unavailable: {error!r}", file=sys.stderr)
        _OCR_REASON = "Text recognition is disabled: the Python package used for recognition is missing."
    else:
        _configure_ocr()
        if not command:
            # With no executable, do not ask for the version—asking would only
            # fail and then report "not installed" as "cannot run", pointing at
            # the wrong half.
            _OCR_REASON = "Text recognition is disabled: the recognition engine executable is not installed."
        else:
            pytesseract.pytesseract.tesseract_cmd = command
            try:
                pytesseract.get_tesseract_version()
                _OCR = pytesseract
            except Exception as error:  # pylint: disable=broad-except
                print(f"[gui] tesseract binary unusable: {error!r}",
                      file=sys.stderr)
                _OCR_REASON = "Text recognition is disabled: the recognition engine executable cannot run."
    if _OCR is None:
        raise GuiError(_OCR_REASON)
    return _OCR


def _ocr_no_language_data(langs: list[str], known: bool) -> bool:
    """True when "the engine can answer with a language list, and the list
    really is empty".

    ⚠️ **This criterion is used in two places, but only one actually calls this
    function.** `ocr_status` calls it directly (to report the whole feature as
    unavailable); `ocr_lang_for` expresses the same degenerate case but **must
    not** be rewritten to call this function—its whitelist must also block "the
    list is non-empty but this code is not in it", which is a wider rule, and
    `known and not langs` is only one special case of it. So the two sides are
    **semantically coupled, not sharing code**: changing one side produces no
    symptom at all (`/sys doctor` says available while every recognition command
    fails hard, which is exactly the inconsistency this criterion exists to
    eliminate), and only the reconciliation test turns red after the fact. That is
    why both sides carry a comment pointing at the other—the only thing that warns
    **beforehand**. `test_gui_control.py` feeds the same `(langs, known)` corpus
    to both callers and compares the answers.

    `known is False` (could not ask) **does not count** as having no language data:
    that half is almost always caused by the engine itself failing to load, which
    the earlier probe reports more precisely; declaring the whole feature
    unavailable because one enumeration hiccuped is exactly what `ocr_lang_for`'s
    whitelist deliberately does not do (see the two ⚠️ there).
    """
    return known and not langs


def ocr_status() -> tuple[bool, str]:
    """`(available?, explanation)`. The explanation is a hardcoded generic string
    that can be sent straight to the user.

    Text recognition needs **three** things, so they are reported separately and
    the user knows which piece to supply: the Python package, the **separately
    installed** recognition engine executable, and at least one language data
    file. The first two are probed by `_load_ocr()`, which also states the reason
    itself (see the two ⚠️ there; `ocr_status` must not keep its own copy of that
    judgement, or the two would each tell a different story—the original three
    duplicated checks were the source of the recursion).

    The third was added on 2026-09-11: from that day `ocr_lang_for` blocks
    `--lang` when "the engine answered and the language list is empty", and the
    path without `--lang` returns `eng` (also not installed) and lets the engine
    raise, so on such a machine the three commands `/locate text find` /
    `/locate text click` / `/locate text wait` **cannot possibly succeed**—while
    `/sys doctor` still showed green at the time. Doctor's advice for this item
    already says "those three commands are all off, image location still works",
    which is honest on a machine with zero languages, so the doctor side needs no
    change.

    **A deliberately accepted trade-off**: the first check, `import pytesseract`,
    used to be re-evaluated every time; it now follows `_load_ocr`'s
    `_OCR_PROBED_CMD`, so "pip-installing the package after the process has
    started" is only noticed after a bot restart. What we get in exchange is "an
    engine installed late is re-probed automatically", which is a far more common
    situation.
    """
    try:
        _load_ocr()
    except GuiError as error:
        # `_load_ocr` only raises one of this module's hardcoded strings
        # (`_OCR_REASON`), so relaying it directly does not violate Secrecy
        # Layer 1; the raw exception only goes to stderr.
        return False, str(error)
    langs, known = ocr_languages()
    if _ocr_no_language_data(langs, known):
        return False, "Text recognition is disabled: language recognition data is missing."
    return True, "Text recognition is available."


def ocr_languages() -> tuple[list[str], bool]:
    """`(installed languages, could we ask?)`.

    ⚠️ **The two values must not be merged into one.** `([], True)` = the engine
    answered and **there really are no languages**; `([], False)` = **could not
    ask** (engine not installed / the enumeration itself failed). Merged, these two
    look identical to the caller, while they need **opposite** handling—that is how
    `ocr_lang_for`'s whitelist silently stopped working:
    `t for t in wanted if available and t not in available`, where an empty list
    short-circuits `available and …` and the whole check disappears without any
    symptom. This project already does the same kind of thing in three places:
    `_process_control._find_all_webrunner_pids`, `discord_bot._load_pid`,
    `discord_bot._webrunner_liveness`.

    `([], True)` is **really reachable**, not theoretical: measured, pointing
    `TESSDATA_PREFIX` at an empty or nonexistent directory gives engine rc=0 and
    `get_languages(config="")` returns `[]` without raising.

    ⚠️ **Do not pass `cached=True` to `get_languages`.** The low level is
    `@run_once`, and it only caches when it receives that flag
    (`pytesseract.py:162`:
    `if not kwargs.pop('cached', False) or wrapper._result is wrapper`). Right now
    it really asks again every time, so after the user adds a language file there is
    no need to restart the bot; with caching, one early `[]` would be frozen for the
    whole lifetime of the process, which under the new rule below amounts to a
    permanent hard block.
    """
    try:
        return sorted(_load_ocr().get_languages(config="")), True
    except Exception:  # pylint: disable=broad-except
        return [], False


def ocr_lang_for(target: str, explicit: str | None = None) -> str:
    """Decide the recognition language.

    By default it is **chosen automatically from the target string**: a user
    looking for a Chinese label (say, the "OK" button) with an English model will
    never find it and gets no clue why. Requiring `--lang chi_tra` every time is the real trap, so a target
    containing CJK characters automatically gets the Chinese model.

    An empty target (the "what does the screen say" use of `read_text`) also gets
    the Chinese model: when you do not know what you will read, use the set with
    the widest coverage.

    The `--lang` whitelist **lets it through when the list cannot be asked for,
    and blocks it when the list really is empty**. Both used to be an empty list
    and took the same path (both let through), while they need opposite handling:

    * **Could not ask (`known is False`) → let through.** The most common cause is
      that the engine is not installed at all, which the downstream `_ocr_call`
      reports as "Text recognition is disabled: …"—a sentence the user can act on.
      Blocking here would only replace it with "No recognition data is installed
      for this language", pointing at the wrong half. The costs are also completely
      asymmetric: one hiccup in the enumeration would make all text recognition
      unusable, whereas letting it through only ends with the engine raising a clear
      error itself (measured, an uninstalled language → `TesseractError`, already
      folded by `_ocr_call` into a generic message, so the tessdata path does not
      leak into the chat either).
    * **The engine answered and the list is empty (`known and not langs`) →
      block.** No recognition can succeed on such a machine, and saying
      "Installed: (none)" outright is better than letting the engine raise a
      generic "Text recognition failed". Before the fix this was **dead code**:
      with an empty list `missing` is always empty too, so the `or "(none)"`
      branch was never reached.
      ⚠️ A known near-miss that is **a chosen trade-off, not an oversight**: the
      low level filters language names with `LANG_PATTERN` (`^[a-z_]+$`,
      `pytesseract.py:51`), so a machine with only capitalised script models
      (`Latin` / `script/Han`) also returns an empty list, and `--lang Latin` is
      blocked along with it. On such a machine this is the only path that could
      work anyway—the automatic language choice would return `eng`, and `eng` is
      not installed.

    The automatic language choice uses `langs` directly: it is `[]` in both cases
    above, and "fall back to eng when you do not know what is there" is its policy
    anyway, not a skipped check.
    """
    langs, known = ocr_languages()
    if explicit:
        wanted = [t.strip() for t in explicit.split("+") if t.strip()]
        if not wanted or any(not HOTKEY_TOKEN_RE.match(t) for t in wanted):
            raise GuiError("Language codes may only contain letters and digits, separated by `+`.")
        # ⚠️ When `langs` is empty, every code lands in `missing`—that degenerate
        # case is `_ocr_no_language_data`, which `ocr_status` uses to report the
        # whole feature as unavailable. This **must not** be changed to call it
        # (the whitelist must also block "the list is non-empty but the code is
        # not in it", a wider rule), so the two sides are semantically coupled,
        # not sharing code; a reconciliation test watches them.
        # `if known:` is deliberately on its own line: skipping the whitelist is a
        # **visible decision**. Writing `if known and any(...)` would recreate the
        # original shape of this defect (a single `X and Y` line, where one false
        # operand quietly switches off the whole check).
        if known:
            missing = [t for t in wanted if t not in langs]
            if missing:
                listed = " / ".join(langs) or "(none)"
                raise GuiError(f"No recognition data is installed for this language. Installed: {listed}")
        return "+".join(wanted)
    if not (target or "").strip() or _CJK_RE.search(target or ""):
        for chinese in ("chi_tra", "chi_sim", "jpn"):
            if chinese in langs:
                return f"{chinese}+eng" if "eng" in langs else chinese
    return "eng" if not langs or "eng" in langs else langs[0]


def _logical_frame(region: list[int] | None):
    """`(image, origin x, origin y)`—one pixel in the image = one click
    coordinate.

    Delegates to the library's `grab_logical`. Limiting to a region **is not just
    filtering the results**; it really captures only that area: the cost of
    recognition and matching is proportional to the pixel count, and a 600×400
    region measured 4 times faster than the whole desktop.
    """
    try:
        return load_ac().grab_logical(_region_xywh(region))
    except GuiError:
        raise
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] grab_logical failed: {error!r}", file=sys.stderr)
        raise GuiError("Screenshot failed.") from error


def _region_xywh(region: list[int] | None) -> tuple[int, int, int, int] | None:
    """This module uses the bbox `[left, top, right, bottom]`; the library wants
    `(x, y, width, height)`."""
    if region is None:
        return None
    left, top, right, bottom = region
    return left, top, right - left, bottom - top


def _ocr_call(name: str, *args, **kwargs) -> Any:
    """Call the library's recognition entry point, folding its exceptions into
    this module's generic `GuiError`.

    "Engine not installed" and "recognition failed" are reported separately: the
    user can fix the former (install the engine), while the latter can only be
    investigated in the log. The type is matched by name rather than imported, to
    avoid pulling in the recognition backend's module at import time just for one
    exception type.
    """
    ac = load_ac()
    _configure_ocr()
    try:
        return getattr(ac, name)(*args, **kwargs)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] ocr {name} failed: {error!r}", file=sys.stderr)
        if type(error).__name__ == "OCRBackendNotAvailableError":
            raise GuiError(ocr_status()[1]) from error
        raise GuiError("Text recognition failed.") from error


def read_text(*, region: list[int] | None = None, lang: str | None = None,
              min_confidence: float = 60.0) -> list[dict[str, Any]]:
    """Read out all the text on the screen (or a given region), one entry per
    line.

    Recognition and coordinate conversion are both in the library; this only
    handles the presentation of "joining words into lines". Lines are decided by
    the library's `group_lines` (grouping by vertical overlap) rather than the
    engine's own line numbers—not every recognition backend reports line numbers.
    """
    ac = load_ac()
    words = _ocr_call("read_text_in_region", region=_region_xywh(region),
                      lang=lang or "eng", min_confidence=min_confidence)
    out: list[dict[str, Any]] = []
    for line in ac.group_lines(words):
        if not line:
            continue
        out.append({
            "text": " ".join(word.text for word in line),
            "x": int(sum(word.center[0] for word in line) / len(line)),
            "y": int(sum(word.center[1] for word in line) / len(line)),
            "confidence": min(word.confidence for word in line),
        })
    return sorted(out, key=lambda row: (row["y"], row["x"]))


def find_text(target: str, *, region: list[int] | None = None,
              min_confidence: float = 60.0, lang: str | None = None,
              case_sensitive: bool = False) -> list[dict[str, Any]]:
    """Find text on the screen, returning `[{text, x, y, confidence}, …]` (click
    coordinates).

    Matching and coordinate conversion are both in the library: within one line
    it finds "the shortest run of consecutive words that, joined together,
    contains the target"—the recognition engine cuts a line into many pieces, and
    matching only single pieces would mean a perfectly ordinary target like
    "Save As" is never found. This module keeps only the **language choice**
    policy (see `ocr_lang_for`) and the conversion of results into the field names
    the caller uses.
    """
    if not (target or "").strip():
        raise GuiError("Give the text to look for.")
    resolved = ocr_lang_for(target, lang)
    matches = _ocr_call("find_text_matches", target, resolved,
                        _region_xywh(region), min_confidence, case_sensitive)
    rows = [{"text": match.text, "x": match.center[0], "y": match.center[1],
             "confidence": match.confidence} for match in matches]
    return sorted(rows, key=lambda row: (row["y"], row["x"]))


def click_text(target: str, *, button: str = "mouse_left",
               region: list[int] | None = None,
               min_confidence: float = 60.0,
               lang: str | None = None) -> tuple[int, int]:
    """Click the text once found, returning the coordinates actually clicked.
    With several matches, click the first one on screen."""
    matches = find_text(target, region=region, min_confidence=min_confidence,
                        lang=lang)
    if not matches:
        raise GuiError("That text was not found on the screen.")
    x, y = matches[0]["x"], matches[0]["y"]
    mouse_click(button, x, y)
    return x, y


def wait_text(target: str, timeout: float, *, region: list[int] | None = None,
              poll: float = 0.5, min_confidence: float = 60.0,
              lang: str | None = None,
              should_abort: Callable[[], bool] | None = None) -> tuple[int, int]:
    """Wait for text to appear on the screen, returning its centre coordinates.
    Raises `GuiError` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matches = find_text(target, region=region, min_confidence=min_confidence,
                            lang=lang)
        if matches:
            return matches[0]["x"], matches[0]["y"]
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and that text still did not appear on the screen.")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# Image location
# --------------------------------------------------------------------------
# The matching itself is handed to the desktop-automation library's
# `match_template_all`: it returns a **similarity score**, uses NMS to merge the
# whole patch of high-scoring points around one target, rejects flat single-colour
# templates, and captures through `grab_logical` (covering every monitor,
# converting back into the click coordinate space). This project once had its own
# copy of each of these; the single source is now on the library side.
#
# What remains here is **de-identifying the error messages**: the library's
# messages are in English and carry the template image's path, and neither may be
# sent straight into the chat platform.
LOCATE_MAX_HITS = 20


def _locate_all(image_path: str, threshold: float,
                region: list[int] | None = None) -> list[tuple[int, int, float]]:
    """Template matching, returning `[(centre x, centre y, similarity), …]`,
    highest similarity first."""
    ac = load_ac()
    try:
        matches = ac.match_template_all(
            str(image_path), region=_region_xywh(region),
            min_score=float(threshold), max_results=LOCATE_MAX_HITS)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] match_template_all failed: {error!r}", file=sys.stderr)
        # A flat single-colour template is something the user can fix (capture an
        # area with a pattern instead), so it is worth spelling out; everything
        # else gets only a generic sentence. Match on the type name to avoid an
        # extra import just for one exception type.
        if type(error).__name__ == "AutoControlFlatTemplateException":
            raise GuiError(
                "The template image is almost a single colour and cannot be located; "
                "please capture an area with a pattern or text."
            ) from error
        if isinstance(error, (OSError, ValueError)):
            raise GuiError("Failed to read the template image (unsupported format?).") from error
        raise GuiError("Image matching failed.") from error
    return [(match.center[0], match.center[1], match.score) for match in matches]


def locate_image(image_path: str, *, threshold: float = 0.9,
                 region: list[int] | None = None) -> tuple[int, int]:
    """Find a template image on the screen, returning the centre coordinates of
    the most similar spot."""
    hits = _locate_all(image_path, threshold, region)
    if not hits:
        raise GuiError("This image was not found on the screen.")
    return hits[0][0], hits[0][1]


def locate_image_all(image_path: str, *, threshold: float = 0.9,
                     region: list[int] | None = None
                     ) -> list[tuple[int, int, float]]:
    """Find every match."""
    return _locate_all(image_path, threshold, region)


def click_image(image_path: str, *, button: str = "mouse_left",
                threshold: float = 0.9,
                region: list[int] | None = None) -> tuple[int, int]:
    """Click the centre of the template image once found; return the
    coordinates."""
    x, y = locate_image(image_path, threshold=threshold, region=region)
    mouse_click(button, x, y)
    return x, y


def wait_image(image_path: str, timeout: float, *, poll: float = 0.6,
               threshold: float = 0.9, region: list[int] | None = None,
               should_abort: Callable[[], bool] | None = None
               ) -> tuple[int, int]:
    """Wait for an image to appear on the screen, returning its centre
    coordinates. Raises `GuiError` on timeout.

    When the recognition engine is not installed, this is the only way to use
    screen content as a synchronisation point (`wait_text` is unavailable).
    """
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        hits = _locate_all(image_path, threshold, region)
        if hits:
            return hits[0][0], hits[0][1]
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and this image still did not appear on the screen.")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# Waiting for "gone" and waiting for a colour
# --------------------------------------------------------------------------
# Waiting for something to **appear** solves only half of the synchronisation
# problem. The other half is more common: waiting for a loading spinner to
# disappear, for a dialog to close, for "Processing" to turn into something else.
# Without this set, those cases can only blindly wait a guessed number of seconds,
# and guessing too short means clicking about before the screen is ready.
def wait_text_gone(target: str, timeout: float, *,
                   region: list[int] | None = None, poll: float = 0.5,
                   min_confidence: float = 60.0, lang: str | None = None,
                   should_abort: Callable[[], bool] | None = None) -> None:
    """Wait for some text to disappear from the screen. Raises `GuiError` on
    timeout."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not find_text(target, region=region, min_confidence=min_confidence,
                         lang=lang):
            return
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and that text is still on the screen.")
        _sleep_abortable(poll, should_abort)


def wait_window_gone(needle: str, timeout: float, poll: float = 0.5, *,
                     should_abort: Callable[[], bool] | None = None) -> None:
    """Wait for the matching window to disappear (close). Raises `GuiError` on
    timeout."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not match_windows(needle):
            return
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and that window is still there.")
        _sleep_abortable(poll, should_abort)


def wait_image_gone(image_path: str, timeout: float, *, poll: float = 0.6,
                    threshold: float = 0.9, region: list[int] | None = None,
                    should_abort: Callable[[], bool] | None = None) -> None:
    """Wait for an image to disappear from the screen. Raises `GuiError` on
    timeout."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not _locate_all(image_path, threshold, region):
            return
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and that image is still on the screen.")
        _sleep_abortable(poll, should_abort)


COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(raw: str) -> tuple[int, int, int]:
    """`"#1E90FF"` / `"1e90ff"` / `"30,144,255"` → `(30, 144, 255)`."""
    text = (raw or "").strip()
    match = COLOR_RE.match(text)
    if match:
        value = match.group(1)
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    if len(parts) == 3:
        try:
            channels = [int(p) for p in parts]
        except ValueError as error:
            raise GuiError("Invalid colour format; use `#RRGGBB` or `r,g,b`.") from error
        if all(0 <= c <= 255 for c in channels):
            return channels[0], channels[1], channels[2]
    raise GuiError("Invalid colour format; use `#RRGGBB` or `r,g,b`.")


PIXEL_TOLERANCE_MAX = 255


def parse_tolerance(raw: str) -> int:
    """Colour tolerance string → an integer in `0..PIXEL_TOLERANCE_MAX` (the error
    allowed per colour channel).

    **The macro's save-time validation and `if_pixel`'s execution must share this
    one function.** The defect measured on 2026-09-21: the validation side read
    `if_pixel`'s fourth argument as seconds (sharing a branch with `wait_pixel`),
    while the execution side read it as an integer tolerance—so
    `if_pixel 10 10 #ffffff 2.5` could be saved and only raised an unexplained
    error when replay reached that line, while a legal tolerance like `… 150` was
    instead blocked at save time by the "seconds cap". Two separate parsers will
    always drift apart again, so only this one is kept.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError("The colour tolerance must be an integer.") from error
    if not 0 <= value <= PIXEL_TOLERANCE_MAX:
        raise GuiError(f"The colour tolerance must be between 0 and {PIXEL_TOLERANCE_MAX}.")
    return value


def wait_pixel(x: int, y: int, color: tuple[int, int, int], timeout: float, *,
               poll: float = 0.3, tolerance: int = 12, match: bool = True,
               should_abort: Callable[[], bool] | None = None
               ) -> tuple[int, int, int]:
    """Wait for a point's colour to become (or stop being) the given colour,
    returning the last colour seen.

    `tolerance` is the error allowed per colour channel—anti-aliasing, colour
    management and video compression all make "the same colour" differ by a few
    steps, and demanding exact equality would make this feature almost never hold
    on a real screen.

    Far cheaper than a screenshot: one pixel vs an image + a recognition pass, so
    it suits synchronisation points polled at a high rate.
    """
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        current = pixel_color(x, y)
        close = all(abs(a - b) <= tolerance for a, b in zip(current, color))
        if close == match:
            return current
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and the colour at that point did not become the expected one."
                           if match else "Timed out and the colour at that point is still the original one.")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# UI element tree (accessibility interface / UI Automation)
# --------------------------------------------------------------------------
# The three location methods so far—coordinates, text recognition, template
# matching—are **all pixel-level guesses**: coordinates break when a window moves,
# recognition breaks with fonts and scaling, template images break when the theme
# changes colour. The OS actually knows which buttons are on the screen, what they
# are called and which rectangle they occupy—that is the accessibility interface's
# data.
#
# So this layer is the **first choice**, not a fallback: use it when it can answer
# (exact rectangles, found even when covered, unaffected by scaling), and only fall
# back to the other three when it cannot. Not every program supports it equally
# well—the trees of old-style window programs, games and some cross-platform
# frameworks may be sparse or even empty, which is normal, not an error.
#
# Traversal and matching are both in the library (`list_accessibility_elements` /
# `find_accessibility_elements` / `control_get_state`). This module keeps only
# three things: validating and hinting type names, converting the library's
# fields into the names the caller uses, and **clicking still through mouse
# coordinates**.
#
# **Limiting to a window (`window`) is not a filter; it changes the search
# starting point.** Without a window it now takes only about 2 seconds (it was
# once 61 seconds), but limiting to a single window takes 0.03 seconds—and does
# not miss windows further down the order. The causes of those three 60-second
# delays were all fixed on the library side: the whole desktop tree was one
# uninterruptible call, a single window's traversal was atomic too, and the OS
# waits for the application to respond (a full-screen game never responds, which
# stalled a single query for 60 seconds).
# The cap on matches returned to the user, and "at most how many elements to look
# at to find them". These are **different numbers**: using the match cap as the
# scan cap amounts to "only look at the first 40 elements on the screen", which
# finds almost nothing.
UI_MAX_RESULTS = 40
UI_SCAN_LIMIT = 1500

# Type names are only used for validation and hints; the actual matching is done
# by the library (it accepts both `button` and the low-level `ControlType_50000`
# spelling).
UI_CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar",
    50038: "separator",
}
_UI_TYPE_CODES = {name: code for code, name in UI_CONTROL_TYPES.items()}


def ui_status() -> tuple[bool, str]:
    """`(available?, explanation)`; the explanation is a hardcoded generic string."""
    if os.name != "nt":
        return False, "UI element location is only available on Windows."
    try:
        ok, reason = load_ac().accessibility_status()
    except GuiError as error:
        return False, str(error)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] accessibility status probe failed: {error!r}",
              file=sys.stderr)
        return False, "UI element location is unavailable in this environment."
    # The reason string comes from the library (English, possibly with install
    # instructions) and is not sent out; it only goes to stderr.
    if not ok:
        print(f"[gui] accessibility unavailable: {reason}", file=sys.stderr)
        return False, "UI element location is unavailable in this environment."
    return True, "UI element location is available."


def parse_ui_type(raw: str) -> str:
    """`"button"` → a validated type name; an empty string returns `""` (any
    type)."""
    key = (raw or "").strip().lower()
    if not key:
        return ""
    if key in _UI_TYPE_CODES:
        return key
    listed = " / ".join(sorted(_UI_TYPE_CODES)[:12])
    raise GuiError(f"Unknown element type. Common ones are: {listed} …")


def _ui_call(func: str, *args, **kwargs) -> Any:
    """Call the library's accessibility entry point, folding its exceptions into
    a generic `GuiError`.

    The first parameter is called `func`, not `name`: these entry points have a
    `name=` keyword argument of their own.
    """
    ac = load_ac()
    try:
        return getattr(ac, func)(*args, **kwargs)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] accessibility {func} failed: {error!r}", file=sys.stderr)
        if type(error).__name__ == "AccessibilityNotAvailableError":
            # "That window cannot be found" and "this machine lacks the feature"
            # both come as this type, but to the user they are two different
            # things—the former is something they can fix.
            if "window title" in str(error):
                raise GuiError("No matching window was found.") from error
            raise GuiError("UI element location is unavailable in this environment.") from error
        raise GuiError("Failed to read the UI elements.") from error


def _ui_row(element: Any) -> dict[str, Any]:
    """A library element → the field names the caller uses."""
    left, top, width, height = element.bounds
    return {
        "name": element.name,
        "type": _ui_type_name(element.role),
        "x": element.center[0],
        "y": element.center[1],
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "enabled": bool(element.enabled),
    }


def _ui_type_name(role: str) -> str:
    """Low-level type code → the short lowercase name this module uses
    (`ControlType_50000` → `button`).

    The library deliberately keeps the raw code (translating it is a separate
    step), but what goes back to the user must be a readable word.
    """
    try:
        return str(load_ac().humanize_role(role) or "").lower()
    except Exception:  # pylint: disable=broad-except
        return str(role or "").lower()


def _ui_visible(element: Any) -> bool:
    """Does it have an actual layout. A zero-area element cannot be clicked, and
    listing it would only mislead."""
    _left, _top, width, height = element.bounds
    return width > 0 and height > 0


def ui_find(name: str, *, control_type: str = "", window: str = "",
            exact: bool = False) -> list[dict[str, Any]]:
    """Find UI elements whose name contains `name`, returning
    `[{name, type, x, y, …}, …]`.

    Matching defaults to "contains" rather than "exactly equal": names in real
    interfaces often carry accelerator markers or suffixes (`Save(&S)`, `OK `),
    and demanding exact equality would leave half the targets unfound. The library
    sorts exact matches first.
    """
    if not (name or "").strip():
        raise GuiError("Give the name of the element to look for.")
    found = _ui_call(
        "find_accessibility_elements", name=name,
        role=parse_ui_type(control_type) or None,
        window_title=window or None, contains=not exact,
        max_results=UI_MAX_RESULTS, scan_limit=UI_SCAN_LIMIT)
    return [_ui_row(element) for element in found if _ui_visible(element)]


def ui_value(name: str, *, control_type: str = "", window: str = "",
             exact: bool = False, limit: int = 5) -> list[dict[str, Any]]:
    """Find elements and attach their current value, returning
    `[{name, type, x, y, value?, toggle?, …}, …]`.

    **Setting a value is not offered.** Writing has the same problem as the
    accessibility interface's Invoke: the program receives the new value but not
    the chain of events for "someone really typed here" (focus, per-character
    changes, losing focus), so validation and dependent logic do not run. To fill
    in an input box, do it the way a person would—click into it, then `!type` /
    `!clip paste`.
    """
    rows = ui_find(name, control_type=control_type, window=window, exact=exact)
    out: list[dict[str, Any]] = []
    for row in rows[:max(1, limit)]:
        merged = dict(row)
        # Ask with `window` included: reading the value walks the tree again, and
        # without a window limit that step covers the whole desktop (measured
        # locally at tens of seconds).
        state = _ui_call("control_get_state", name=row["name"],
                         window_title=window or None)
        merged.update(state or {})
        out.append(merged)
    return out


def ui_tree(window: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """List the UI elements (under a given window), so a person can see clearly
    what there is to click."""
    found = _ui_call("list_accessibility_elements",
                     window_title=window or None, max_results=max(1, limit))
    return [_ui_row(element) for element in found
            if _ui_visible(element) and element.name]


def ui_click(name: str, *, button: str = "mouse_left", control_type: str = "",
             window: str = "") -> tuple[int, int]:
    """Click the element's centre once found; return the coordinates.

    It deliberately **clicks the coordinates with the mouse** rather than calling
    the accessibility interface's Invoke: Invoke does not need the element to be on
    screen, but for that very reason it bypasses the program's judgement of
    "someone really clicked here" (hover state, focus, drag and drop). This project
    simulates a person's actions, so it only uses the interface to get the exact
    position, and the action still goes through the mouse.
    """
    matches = ui_find(name, control_type=control_type, window=window)
    if not matches:
        raise GuiError("No UI element with that name was found.")
    target = matches[0]
    mouse_click(button, target["x"], target["y"])
    return target["x"], target["y"]


def ui_wait(name: str, timeout: float, *, control_type: str = "",
            window: str = "", poll: float = 0.5, gone: bool = False,
            should_abort: Callable[[], bool] | None = None
            ) -> dict[str, Any] | None:
    """Wait for a UI element to appear (or disappear). Raises `GuiError` on
    timeout."""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matches = ui_find(name, control_type=control_type, window=window)
        if gone and not matches:
            return None
        if not gone and matches:
            return matches[0]
        if time.monotonic() >= deadline:
            raise GuiError("Timed out and that UI element is still there." if gone
                           else "Timed out and this UI element still did not appear.")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# Files in and out of the host
# --------------------------------------------------------------------------
# Without this section, "do anything through chat" is missing a piece: command
# execution can "act", but cannot **fetch a file back to look at** or **put a file
# up**. `!sh` output is de-identified and length-capped, so using it as a file
# transfer channel is not workable.
#
# The caps are set separately: fetching is limited by the chat platform's
# attachment size, uploading only by the disk.
GET_MAX_BYTES = 20 * 1024 * 1024
PUT_MAX_BYTES = 64 * 1024 * 1024


def unquote_path(raw: str | None) -> str:
    """Normalise a path string the user pasted in: strip surrounding whitespace,
    then peel off one layer of **paired** quotes.

    It exists because File Explorer's "Copy as path" (Shift + right-click) comes
    with double quotes, so what gets pasted is `"D:\\Work\\Foo"`—which is not any
    path that exists.

    Only one **paired** layer is peeled. This (and two other places) used to read
    `.strip('"').strip("'")`, which scrapes off **every** quote character at both
    ends: a directory really named `'foo'` (`'` is legal in Windows file names)
    would silently become `foo`, so `/host put` and `/host cd` point somewhere
    else—**silently pointing at the wrong place is worse than an outright
    rejection**, because this path is the owner-only "operate the whole computer
    through chat", and writing to the wrong place really is writing to the wrong
    place. When the quotes are not paired, leave it as-is and let it fail to parse
    naturally, rather than guessing what the user meant.

    This and `dorossi_backend._dorossi_unquote_dir` are two implementations of the
    same criterion (different module boundaries, no mutual import). When changing
    one, remember to look at the other.
    """
    text = (raw or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            return text[1:-1].strip()
    return text


def resolve_host_path(raw: str) -> Path:
    """Turn the path the user typed into an absolute path. Relative paths are
    based on the project root.

    It deliberately **does no sandboxing**: the callers of this capability are
    owner-only, and restricting it to the project directory would make "operate
    the whole computer through chat" a misnomer. The real gate is the caller's
    identity check.
    """
    text = unquote_path(raw)
    if not text:
        raise GuiError("Give a file path.")
    expanded = os.path.expandvars(os.path.expanduser(text))
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.normpath(str(path)))


# The **single source** for the Windows reserved-name criterion **is the standard
# library** (`ntpath.isreserved`, 3.13+). The 30 device names are deliberately not
# copied here: that data grows with Windows versions (`COM¹` / `COM²` / `COM³`
# were only added in Windows 11), while a copy would not—this repo already has the
# precedent of three copies of `_pid_alive`. `ntpath` rather than `os.path`:
# `os.path.isreserved` only exists on Windows, while the write target is always a
# Windows host, so the criterion must not depend on "the platform running the
# tests".
#
# It is taken as a module-level alias so that an interpreter lacking it blows up
# **at import time**. Written inside the function, the `AttributeError` would be
# folded by `cmd_put`'s broad except into a "file write failed"—the guard would
# silently vanish, which is exactly why this guard exists.
_isreserved_name = ntpath.isreserved


def _reject_reserved_filename(name: str) -> None:
    """Raise `GuiError` if Windows would treat `name` specially.

    `ntpath.isreserved` answers three things at once, and what they have in common
    is that **writing to it will not produce the file the user wanted**:

    1. DOS device names (`NUL` / `CON` / `COM1`…, case-insensitive; `NUL.md`
       counts too);
    2. the reserved characters `*?"<>/\\:|` and ASCII control characters;
    3. trailing dots and spaces.

    **Only trailing dots and spaces are stripped before asking**, rather than
    taking "the segment before the first dot". Both approaches get around rule 3
    (this project has already ruled that all-dot names pass through as-is, see
    below), but taking the segment would let rule 2 **only see what is before the
    first dot**. Measured, that differs by 8 cases, and the ones that differ are
    exactly the ones most worth blocking: writing `report.txt:secret` creates an
    NTFS alternate data stream—`os.listdir` cannot see it, `report.txt` itself is
    **0 bytes**, and the caller reports success. Reporting success while the data
    is invisible is the kind of silent error this project cares about most. In the
    same family are `a.txt\\tb`, `a.b<c`, `a.b|c`.

    After stripping trailing dots and spaces, `...` / `....` / `x.` become `` /
    `x`, neither of which is reserved, so they pass through as-is—that is an
    existing ruling, not an oversight:
    `test_bot_helpers.test_the_attachment_name_guard_lets_nothing_escape` pins
    all-dot names as passing, because writing them makes `os.replace` raise errno
    13 and fail **loudly**, rather than silently writing to the wrong place. To
    change that decision, change that test.

    ⚠️ **This guard is deliberately wider than "the names that really cause
    trouble on this machine"; do not narrow it to match measurements.** Measured on
    2026-09-11 with `CreateFileW` + `GetFileType` (Windows 11 build 26200): only
    the `NUL` family is really rewritten by the path parser to `\\\\.\\NUL`, while
    `CON` / `PRN` / `AUX` / `COM1` / `LPT1` / `CONIN$` all produce an **ordinary
    file** of `type=DISK`. Older Windows versions were not like this, and the
    standard library's own comment says "the rules are complex and vary by version,
    so to be safe always return True". Dropping `CON` because it turned out fine on
    this machine would make `write_host_file`'s behaviour follow the host's Windows
    version—a pit a fresh clone would fall into while this machine never sees it.
    """
    if _isreserved_name(name.rstrip(". ")):
        raise GuiError(
            "Windows treats this file name specially (a device name like `NUL` / `CON` / `COM1`, "
            "or it contains `: * ? \" < > |` or control characters), so writing it will not produce "
            "the file you want—the data may vanish or go into an invisible data stream. "
            "Please use a different file name.")


def safe_basename(name: str) -> str:
    """Extract a clean file name from a name given by the user / an attachment,
    blocking path traversal and reserved names."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        raise GuiError("Invalid file name.")
    _reject_reserved_filename(base)
    return base


def read_host_file(raw: str) -> tuple[Path, bytes]:
    """Read a file on the host, returning `(path, content)`."""
    path = resolve_host_path(raw)
    if path.is_dir():
        raise GuiError("That is a folder, not a file.")
    if not path.exists():
        raise GuiError("File not found.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise GuiError("Failed to read the file.") from error
    if size > GET_MAX_BYTES:
        raise GuiError(f"The file is too large (limit {GET_MAX_BYTES // (1024 * 1024)} MB).")
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise GuiError("Failed to read the file.") from error


def write_host_file(raw: str, data: bytes, *, default_name: str = "",
                    overwrite: bool = False) -> tuple[Path, int]:
    """Write content to the host, returning `(path, byte count)`.

    * When the destination is an existing folder (or ends with a slash),
      `default_name` is used as the file name;
    * if the parent folder does not exist it fails outright, **without creating
      the whole tree**—one typo would leave a string of empty directories, and the
      person who issued the command is not at the computer to see them;
    * an existing file is only replaced with `overwrite`;
    * writing goes through a same-directory temp + `os.replace` (cross-process
      files are always written atomically, see CLAUDE.md).
    """
    if len(data) > PUT_MAX_BYTES:
        raise GuiError(f"The file is too large (limit {PUT_MAX_BYTES // (1024 * 1024)} MB).")
    # There is only one normalisation (`unquote_path`). `resolve_host_path` calls
    # it too—it is idempotent, so calling it twice is harmless; the normalised
    # string is needed here for the `endswith` on the next line.
    text = unquote_path(raw)
    path = resolve_host_path(text)
    if path.is_dir() or text.endswith(("/", "\\")):
        if not default_name:
            raise GuiError("The destination is a folder; please give a full file name.")
        path = path / safe_basename(default_name)
    # The explicit-path route (`/host put D:\x\NUL`) **does not go through**
    # `safe_basename`, so ask again here—idempotent, for the same reason as calling
    # `unquote_path` twice above.
    #
    # ⚠️ The order is load-bearing: this must block **before** `path.exists()`.
    # `Path(r"…\NUL").exists()` is True, so blocking late would first give the user
    # "The destination file already exists; add `--force` to overwrite it."—a
    # sentence steering them to `--force`, after which `--force` would only get
    # "Failed to write the file." (the actual outcome measured on 2026-09-11: both
    # sentences are wrong answers, and this is a command an owner uses to operate a
    # machine they are not physically at).
    _reject_reserved_filename(path.name)
    if not path.parent.is_dir():
        raise GuiError("The destination folder does not exist.")
    if path.exists() and not overwrite:
        raise GuiError("The destination file already exists; add `--force` to overwrite it.")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as error:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise GuiError("Failed to write the file.") from error
    return path, len(data)


# --------------------------------------------------------------------------
# Macros
# --------------------------------------------------------------------------
MACRO_MAX_STEPS = 200
MACRO_MAX_WAIT_SEC = 120.0
MACRO_SCHEMA_VERSION = 1

# Each verb → the number of arguments it needs `(min, max)`; `None` means no limit.
# `sh` is deliberately **not** in this table: a macro is stored on disk and can be
# replayed by anyone entitled to issue commands, so hiding arbitrary command
# execution in a macro amounts to building a "stored remote-execution backdoor"
# that bypasses `!sh`'s owner gate. To run a command, type `!sh` yourself.
# Condition verb → `(predicate, negated?)`. The eight form mechanical pairs, and
# the implementation takes one path for all of them. **Their costs differ a lot**,
# and the docs must say so clearly: `if_pixel` is almost free, `if_window` /
# `if_ui` take milliseconds to a second, `if_text` runs a recognition pass (3
# seconds and up for the whole desktop). One wrong condition inside a `repeat`
# turns the whole macro from seconds into minutes.
MACRO_CONDITIONS: dict[str, tuple[str, bool]] = {
    "if_text": ("text", False), "if_no_text": ("text", True),
    "if_window": ("window", False), "if_no_window": ("window", True),
    "if_ui": ("ui", False), "if_no_ui": ("ui", True),
    "if_pixel": ("pixel", False), "if_no_pixel": ("pixel", True),
}

MACRO_VERBS: dict[str, tuple[int, int | None]] = {
    # --- control flow ---
    "repeat": (1, 1),       # count
    "else": (0, 0),
    "end": (0, 0),
    "stop": (0, 0),         # end the whole macro early (not a failure)
    "call": (1, None),      # macro name [args…]
    **{verb: ((3, 4) if verb.endswith("pixel") else (1, None))
       for verb in MACRO_CONDITIONS},
    # --- actions ---
    "click": (2, 3),        # x y [button]
    "move": (2, 2),         # x y
    "drag": (4, 5),         # x1 y1 x2 y2 [button]
    "scroll": (1, 3),       # amount [x y]
    "dclick": (2, 3),       # x y [button]
    "type": (1, None),      # text…
    "paste": (1, None),     # text…
    "hotkey": (1, 1),       # combo
    "keydown": (1, 1),      # key name (held down)
    "keyup": (1, 1),        # key name
    "release_keys": (0, 0),  # release everything
    "focus": (1, None),     # window title fragment
    "win": (2, None),       # action window-title fragment
    "wait": (1, 1),         # seconds
    "wait_window": (1, None),   # [timeout] title fragment — see the parse notes
    "wait_text": (1, None),
    "click_text": (1, None),
    "clip": (1, None),      # set <text>
    "ui_click": (1, None),      # element name
    "wait_ui": (1, None),       # [seconds] element name
    "wait_gone_text": (1, None),
    "wait_gone_window": (1, None),
    "wait_pixel": (3, 4),       # x y colour [seconds]
}

# The most steps one run may execute. `repeat` can nest, so a cap on source lines
# alone cannot stop an endless loop—a `repeat 1000` wrapping a `repeat 1000` is
# only four lines but runs a million steps.
MACRO_MAX_EXECUTED = 5000
MACRO_MAX_REPEAT = 1000
MACRO_MAX_CALL_DEPTH = 3
# `$1`..`$9` are arguments, `$$` is a literal `$`; the full syntax is in
# `substitute_macro_args`.
MACRO_ARG_RE = re.compile(r"\$(\$|[1-9])")

# The most steps the up-front check (`check_macro_program`) validates. With
# memoisation on `(macro name, args)`, the workload for the common style of
# "passing the arguments down unchanged" is "number of distinct macros × steps";
# but memoisation is **not** a bound: every level can split off distinct keys with
# literal arguments (`call b 1` … `call b 200`, and inside `b`,
# `call c $1 1` … `call c $1 200`), so three levels make eight million distinct
# keys and over a billion validations. The execution side is guarded by
# `MACRO_MAX_EXECUTED`; the up-front check needs a cap of its own, or checking
# would take longer than running, stuck in a worker thread nobody can reclaim.
# Twenty thousand steps = a hundred distinct fully loaded macros, which normal use
# never reaches. Measured 2026-09-21: a fan-out of four levels of 200 lines each,
# passing arguments down unchanged, validates 1,400 times with memoisation (1.6
# billion without) in 0.03 seconds; a three-level fan-out with literal arguments
# hits this cap and finishes in 0.07 seconds.
MACRO_MAX_CHECKED = 20000

# Verbs that may lead with a timeout in seconds → the default seconds when none is
# given. **Save-time validation and execution share this table**: `run_macro_step`
# takes the default from here and hands it to `split_timeout`, and
# `validate_macro_step` parses the same set of verbs once up front with the same
# `split_timeout`. When the two sides each kept their own list, the validation side
# only remembered `wait_window` / `wait_text`, so `wait_ui 150 OK`,
# `wait_gone_text -1 x` and `click_text nan x` could all be saved and only blew up
# when replay reached that line (measured 2026-09-21).
# `click_text`'s seconds are discarded (a one-shot recognition has no wait loop),
# but it still goes through parsing, so bad values must be blocked at save time
# just the same.
MACRO_TIMEOUT_DEFAULTS: dict[str, float] = {
    "wait_window": 15.0,
    "wait_text": 15.0,
    "click_text": 0.0,
    "wait_ui": 15.0,
    "wait_gone_text": 15.0,
    "wait_gone_window": 15.0,
}


def parse_macro_steps(raw: str) -> list[str]:
    """Turn the multi-line text the user pasted into a normalised list of steps,
    validating it along the way.

    One step per line; lines starting with `#` and blank lines are ignored.
    Validation is done once **at save time**, so a broken macro does not blow up
    halfway through a replay; `load_macro` validates again, because the file on
    disk can be edited by hand.
    """
    steps: list[str] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # Let the user paste `!` commands straight in without stripping the
        # exclamation mark line by line
        if text.startswith("!"):
            text = text[1:].strip()
        try:
            validate_macro_step(text)
        except GuiError as error:
            raise GuiError(f"Line {line_no}: {error}") from error
        steps.append(text)
        if len(steps) > MACRO_MAX_STEPS:
            raise GuiError(f"A macro may have at most {MACRO_MAX_STEPS} steps.")
    if not steps:
        raise GuiError("The macro is empty.")
    macro_block_map(steps)   # check block balance at save time, not halfway through a replay
    return steps


def macro_block_map(steps: list[str]) -> dict[int, tuple[int | None, int]]:
    """Map each `repeat` / `if_*` to its `else` (if any) and `end`.

    The block structure is validated once **at save time**: a missing `end` is an
    easy mistake to make, and if it is only discovered halfway through a replay,
    the steps before it have already been done on the real desktop and cannot be
    taken back.

    Returns `{opening line index: (else line index or None, end line index)}`.
    """
    stack: list[tuple[int, str]] = []
    blocks: dict[int, tuple[int | None, int]] = {}
    elses: dict[int, int] = {}
    for index, step in enumerate(steps):
        verb = (step or "").split()[0].lower() if (step or "").split() else ""
        if verb == "repeat" or verb in MACRO_CONDITIONS:
            stack.append((index, verb))
        elif verb == "else":
            if not stack or stack[-1][1] not in MACRO_CONDITIONS:
                raise GuiError(f"Line {index + 1}: `else` must be inside an `if_…` block.")
            if stack[-1][0] in elses:
                raise GuiError(f"Line {index + 1}: an `if_…` may have only one `else`.")
            elses[stack[-1][0]] = index
        elif verb == "end":
            if not stack:
                raise GuiError(f"Line {index + 1}: an extra `end`.")
            opener, _kind = stack.pop()
            blocks[opener] = (elses.get(opener), index)
    if stack:
        line = stack[-1][0] + 1
        raise GuiError(f"The block on line {line} has no matching `end`.")
    return blocks


def validate_macro_step(step: str) -> tuple[str, list[str]]:
    """Validate a single step, returning `(verb, args)`. Raise `GuiError` if it
    is invalid."""
    parts = (step or "").split()
    if not parts:
        raise GuiError("Empty step.")
    verb = parts[0].lower()
    args = parts[1:]
    if verb not in MACRO_VERBS:
        allowed = " / ".join(sorted(MACRO_VERBS))
        raise GuiError(f"Unknown action `{verb}`. Available: {allowed}")
    low, high = MACRO_VERBS[verb]
    if len(args) < low or (high is not None and len(args) > high):
        raise GuiError(f"Wrong number of arguments for `{verb}`.")
    # Per-verb detailed checks—block at save time rather than failing on replay
    if verb in ("click", "move", "dclick"):
        parse_xy(args[:2])
        if len(args) > 2:
            parse_button(args[2])
    elif verb == "drag":
        parse_xy(args[:2])
        parse_xy(args[2:4])
        if len(args) > 4:
            parse_button(args[4])
    elif verb == "scroll":
        try:
            int(args[0])
        except ValueError as error:
            raise GuiError("The first argument of `scroll` must be an integer.") from error
        if len(args) == 3:
            parse_xy(args[1:3])
        elif len(args) == 2:
            raise GuiError("`scroll` takes either no coordinates or two.")
    elif verb == "hotkey":
        parse_hotkey_tokens(args[0])
    elif verb in ("keydown", "keyup"):
        parse_key_name(args[0])
    elif verb == "repeat":
        try:
            count = int(args[0])
        except ValueError as error:
            raise GuiError("The `repeat` count must be an integer.") from error
        if not 0 <= count <= MACRO_MAX_REPEAT:
            raise GuiError(f"The `repeat` count must be between 0 and {MACRO_MAX_REPEAT}.")
    elif verb == "call":
        macro_path(args[0])          # name validity (also blocks path traversal)
    elif verb in ("if_pixel", "if_no_pixel"):
        # The fourth argument is the **colour tolerance** (an integer), not
        # seconds—it goes through the same `parse_tolerance` as
        # `eval_macro_condition`; do not merge it back into the `wait_pixel`
        # branch below.
        parse_xy(args[:2])
        parse_color(args[2])
        if len(args) > 3:
            parse_tolerance(args[3])
    elif verb == "wait_pixel":
        # Here the fourth argument really is the timeout in seconds (see
        # `run_macro_step`)
        parse_xy(args[:2])
        parse_color(args[2])
        if len(args) > 3:
            parse_duration(args[3], maximum=MACRO_MAX_WAIT_SEC)
    elif verb == "wait":
        parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC)
    elif verb == "win":
        action = args[0].lower()
        if action != "close" and action not in WINDOW_SHOW_ACTIONS:
            raise GuiError("The `win` action must be `min` / `max` / `restore` / `show` / `hide` / `close`.")
    elif verb in MACRO_TIMEOUT_DEFAULTS:
        # If the first argument is a number it is the timeout in seconds, and the
        # rest is the target. Call the same `split_timeout` the execution side
        # uses; do not write a second "does it look like a number" check here.
        split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
    elif verb == "clip":
        if args[0].lower() != "set" or len(args) < 2:
            raise GuiError("In a macro, `clip` can only be `clip set <text>`.")
    return verb, args


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def run_macro_step(step: str, *,
                   should_abort: Callable[[], bool] | None = None) -> str:
    """Run a single step, returning a sentence that can be shown to the user
    directly (**synchronous, blocking**).

    `should_abort` is passed down to every action that waits. Without it, "the
    abort takes effect between steps" would mean "the abort takes effect after at
    most `MACRO_MAX_WAIT_SEC`"—a single `wait_text 120 …` could leave `!macro stop`
    unresponsive for two minutes.
    """
    verb, args = validate_macro_step(step)
    if verb == "click":
        x, y = parse_xy(args[:2])
        button = parse_button(args[2]) if len(args) > 2 else "mouse_left"
        mouse_click(button, x, y)
        return f"clicked ({x}, {y})"
    if verb == "dclick":
        x, y = parse_xy(args[:2])
        button = parse_button(args[2]) if len(args) > 2 else "mouse_left"
        mouse_click(button, x, y, times=2)
        return f"double-clicked ({x}, {y})"
    if verb == "move":
        x, y = parse_xy(args[:2])
        mouse_move(x, y)
        return f"moved to ({x}, {y})"
    if verb == "drag":
        x1, y1 = parse_xy(args[:2])
        x2, y2 = parse_xy(args[2:4])
        button = parse_button(args[4]) if len(args) > 4 else "mouse_left"
        mouse_drag(x1, y1, x2, y2, button)
        return f"dragged ({x1}, {y1}) → ({x2}, {y2})"
    if verb == "scroll":
        amount = int(args[0])
        if len(args) == 3:
            x, y = parse_xy(args[1:3])
            mouse_scroll(amount, x, y)
        else:
            mouse_scroll(amount)
        return f"scrolled {amount}"
    if verb == "type":
        text = " ".join(args)
        type_text(text)
        return f"typed {len(text)} characters"
    if verb == "paste":
        text = " ".join(args)
        paste_text(text)
        return f"pasted {len(text)} characters"
    if verb == "hotkey":
        tokens = parse_hotkey_tokens(args[0])
        press_hotkey(tokens)
        return f"pressed {' + '.join(tokens)}"
    if verb == "keydown":
        return f"holding {key_down(args[0])}"
    if verb == "keyup":
        return f"released {key_up(args[0])}"
    if verb == "release_keys":
        # This is a step description for a person to read, so it goes through
        # `_report`: keys that could not be released are not counted as
        # "released", nor can they go unmentioned—they are still listed in
        # `/input key status`, and the end of the macro will try again.
        released, stuck = release_all_inputs_report()
        detail = f"{len(released)} released"
        if stuck:
            detail += f"; {len(stuck)} could not be released"
        return f"released all keys ({detail})"
    if verb == "focus":
        _hwnd, _title, count = window_focus(" ".join(args))
        return f"focused window ({count} matched)"
    if verb == "win":
        action = args[0].lower()
        needle = " ".join(args[1:])
        if action == "close":
            _hwnd, _title, count = window_close(needle)
        else:
            _hwnd, _title, count = window_show(needle, action)
        return f"window {action} ({count} matched)"
    if verb == "wait":
        seconds = parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC)
        _sleep_abortable(seconds, should_abort)
        return f"waited {seconds:g} seconds"
    if verb == "wait_window":
        timeout, needle = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_window(needle, timeout, should_abort=should_abort)
        return "window appeared"
    if verb == "wait_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        x, y = wait_text(target, timeout, should_abort=should_abort)
        return f"text appeared at ({x}, {y})"
    if verb == "click_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        del timeout
        x, y = click_text(target)
        return f"clicked text at ({x}, {y})"
    if verb == "clip":
        text = " ".join(args[1:])
        set_clipboard(text)
        return f"wrote {len(text)} characters to the clipboard"
    if verb == "ui_click":
        x, y = ui_click(" ".join(args))
        return f"clicked UI element at ({x}, {y})"
    if verb == "wait_ui":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        found = ui_wait(target, timeout, should_abort=should_abort)
        return f"UI element appeared at ({found['x']}, {found['y']})"
    if verb == "wait_gone_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_text_gone(target, timeout, should_abort=should_abort)
        return "text disappeared"
    if verb == "wait_gone_window":
        timeout, needle = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_window_gone(needle, timeout, should_abort=should_abort)
        return "window closed"
    if verb == "wait_pixel":
        x, y = parse_xy(args[:2])
        color = parse_color(args[2])
        timeout = (parse_duration(args[3], maximum=MACRO_MAX_WAIT_SEC)
                   if len(args) > 3 else 15.0)
        wait_pixel(x, y, color, timeout, should_abort=should_abort)
        return f"({x}, {y}) turned the specified colour"
    raise GuiError(f"Unknown action `{verb}`.")


def eval_macro_condition(verb: str, args: list[str]) -> bool:
    """Evaluate an `if_…` condition."""
    kind, negate = MACRO_CONDITIONS[verb]
    if kind == "text":
        result = bool(find_text(" ".join(args)))
    elif kind == "window":
        result = bool(match_windows(" ".join(args)))
    elif kind == "ui":
        result = bool(ui_find(" ".join(args)))
    else:
        x, y = parse_xy(args[:2])
        color = parse_color(args[2])
        # The same parser as `validate_macro_step`; any value allowed at save time
        # is guaranteed to be understood here
        tolerance = parse_tolerance(args[3]) if len(args) > 3 else 12
        current = pixel_color(x, y)
        result = all(abs(a - b) <= tolerance for a, b in zip(current, color))
    return (not result) if negate else result


def split_timeout(args: list[str], *, default: float) -> tuple[float, str]:
    """`["10", "Save"]` → `(10.0, "Save")`; `["Save"]` → `(default, "Save")`."""
    if len(args) > 1 and _looks_numeric(args[0]):
        return parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC), " ".join(args[1:])
    return default, " ".join(args)


def macro_path(name: str) -> Path:
    """The macro file path. Names are limited to letters, digits / `_` / `-`,
    blocking path traversal such as `../`."""
    key = (name or "").strip()
    if not MACRO_NAME_RE.match(key):
        raise GuiError("A macro name may only contain letters, digits, underscores and hyphens, "
                       "length 1..40.")
    return MACRO_DIR / f"{key}.json"


def _atomic_write(path: Path, content: str) -> None:
    """Same-directory temp → `os.replace` (cross-process files are always written
    atomically, see CLAUDE.md)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise


def _macro_author_id(raw: Any) -> int:
    """Normalise the on-disk `author_id` into a plain non-negative `int`; anything
    broken returns 0.

    `save_macro` writes five fields, and although `load_macro`'s docstring says
    "re-validate every step", it actually only re-validated `steps`. `author_id` is
    passed to `int(...)` by `edit_macro`, and a macro file on disk can be broken by
    hand: `"abc"` → `ValueError`, `[1, 2]` / `{"a": 1}` → `TypeError`, `Infinity`
    → `OverflowError`, `NaN` → `ValueError`. The `/macro` side only does
    `except GuiError`, so these would bubble all the way up to the dispatch layer's
    generic failure sentence, and the user **could never again fix that macro
    through the bot**—while the whole reason this command set exists is that "the
    person issuing commands is not at the computer".

    The `1e400` kind does not even need anyone to edit the file by hand: a large
    enough number comes out of `json.loads` as `inf` (this project has already
    written a whole batch of guards for the same family of numbers).

    A broken value falls back to 0 (= unknown author) rather than raising
    `GuiError`: `steps` is behaviour and cannot run when broken; `author_id` is only
    an annotation—rejecting the whole macro over an annotation would close off the
    repair path too, which is exactly the shape of the defect being fixed.

    `bool` has to be excluded separately: `isinstance(True, int)` is True, so
    `true` would pass a naive `isinstance(x, int)` gate and quietly record the
    author as user 1 (an id that really exists). This is the fourth instance of the
    same trap in this project.

    There is deliberately **no upper bound**: the platform's user ids are 64-bit,
    `400000000000000001` is a legal value, and adding a casual cap would cut a real
    author down to 0.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw >= 0 else 0


def save_macro(name: str, steps: list[str], *, author_id: int = 0) -> Path:
    path = macro_path(name)
    payload = {
        "version": MACRO_SCHEMA_VERSION,
        "name": path.stem,
        "created": time.time(),
        "author_id": _macro_author_id(author_id),
        "steps": list(steps),
    }
    try:
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as error:
        raise GuiError("Failed to save the macro.") from error
    return path


def load_macro(name: str) -> dict[str, Any]:
    """Read a macro and **re-validate** every step—the file on disk can be broken
    by hand."""
    path = macro_path(name)
    if not path.exists():
        raise GuiError("No macro with that name was found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GuiError("Failed to read the macro file, or its format is corrupt.") from error
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        raise GuiError("The macro file's content is invalid.")
    if len(steps) > MACRO_MAX_STEPS:
        raise GuiError("The macro has more steps than the limit.")
    clean: list[str] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, str):
            raise GuiError(f"Macro step {index} is not text.")
        try:
            validate_macro_step(step)
        except GuiError as error:
            raise GuiError(f"Macro step {index} is invalid: {error}") from error
        clean.append(step)
    macro_block_map(clean)
    data["steps"] = clean
    # Fields other than `steps` can be edited by hand too. `version` / `name` /
    # `created` are regenerated by `save_macro` on every write-back, so a broken one
    # never flows downstream; `author_id` is different—its original value is
    # carried back into `save_macro`, so it is normalised **on the read side**,
    # which benefits every caller.
    data["author_id"] = _macro_author_id(data.get("author_id"))
    return data


def edit_macro(name: str, line: int, step: str | None, *,
               insert: bool = False) -> list[str]:
    """Replace / insert / delete one line of a macro, returning the edited list of
    steps.

    `step is None` means delete. Line numbers start at 1 (matching what
    `/macro show` displays). After the edit, **the whole macro is re-validated**
    (per-line validity + block balance) before writing back: validating only that
    line would let a deleted `end` blow up the whole macro on the next replay.
    """
    data = load_macro(name)
    steps = list(data["steps"])
    if insert:
        if not 1 <= line <= len(steps) + 1:
            raise GuiError(f"The line number must be between 1 and {len(steps) + 1}.")
    elif not 1 <= line <= len(steps):
        raise GuiError(f"The line number must be between 1 and {len(steps)}.")
    if step is None:
        steps.pop(line - 1)
    elif insert:
        steps.insert(line - 1, step.strip())
    else:
        steps[line - 1] = step.strip()
    if not steps:
        raise GuiError("A macro cannot become empty; to delete it entirely, use `/macro delete`.")
    if len(steps) > MACRO_MAX_STEPS:
        raise GuiError(f"A macro may have at most {MACRO_MAX_STEPS} steps.")
    for index, entry in enumerate(steps, start=1):
        try:
            validate_macro_step(entry)
        except GuiError as error:
            raise GuiError(f"Step {index} is invalid: {error}") from error
    macro_block_map(steps)
    # `load_macro` has already normalised it into a non-negative `int`, so there is
    # no need to `int(...)` it again here.
    save_macro(name, steps, author_id=data.get("author_id", 0))
    return steps


def list_macros() -> list[tuple[str, int, float]]:
    """`[(name, step count, mtime), …]`, sorted by name. Broken files are skipped
    without blocking the list."""
    if not MACRO_DIR.exists():
        return []
    out: list[tuple[str, int, float]] = []
    for path in sorted(MACRO_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            steps = data.get("steps") if isinstance(data, dict) else []
            count = len(steps) if isinstance(steps, list) else 0
            out.append((path.stem, count, path.stat().st_mtime))
        except (OSError, ValueError):
            continue
    return out


def delete_macro(name: str) -> None:
    path = macro_path(name)
    if not path.exists():
        raise GuiError("No macro with that name was found.")
    try:
        path.unlink()
    except OSError as error:
        raise GuiError("Failed to delete the macro.") from error


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------
# The `run_shell` child processes currently running. `!sh` is synchronous and
# blocking, so without this list there is no way to abort one.
_SHELL_PROCS: set = set()

SHELL_DEFAULT_TIMEOUT_SEC = 60.0
SHELL_MAX_TIMEOUT_SEC = 900.0
SHELL_MAX_OUTPUT_CHARS = 200_000

# --------------------------------------------------------------------------
# PowerShell encoding: unify it at the source, rather than picking a decoder at the
# reading end
# --------------------------------------------------------------------------
# One `/host sh run` output stream **can carry two encodings at once**, so "pick a
# decoder" was never a workable approach (measured, not inferred):
#
# * PowerShell's **own** output (`Write-Output '佇列已清空，行程結束'`) goes through
#   the **console code page**, cp950 locally—the same for `pwsh` 7.6.6 and the
#   built-in `powershell` 5.1. Read with `encoding="utf-8"`, those 10 Chinese
#   characters become **11 U+FFFD**.
# * **Native command** output (`git log --format=%s`) is bytes PowerShell **passes
#   through unchanged**; git emits UTF-8, so reading that half as utf-8 is right,
#   and reading it as `"oem"` fails to decode entirely.
#
# So PowerShell is instead told to use UTF-8 on both ends. `errors="replace"`
# guarantees this defect never shows up as an exception; it only silently eats the
# Chinese—no red text, no traceback.
#
# ⚠️ **The `InputEncoding` line was not added in passing; the input side was
# broken to begin with.** We write `job_start(interactive=True)`'s stdin encoded as
# UTF-8, while the PowerShell end decodes it with the console code page. Measured:
# feeding in `測試輸入` (4 characters, 12 bytes in UTF-8), `Read-Host` received
# **6 characters** (U+769C U+7948 U+5CAB U+981B U+8A68 U+F16F).
# **And the echo hides the damage**: that mojibake is encoded back to stdout with
# the same code page, the bytes are identical to the original UTF-8, and decoding
# them as utf-8 "looks completely correct". What must be measured is `$x.Length`,
# not what the echo looks like.
#
# ⚠️⚠️ **These two setters hit "the shared console", and are never restored.**
# Underneath they call `SetConsoleOutputCP` / `SetConsoleCP`, which change the code
# page of **the caller's** console. Without the `_SHELL_CREATIONFLAGS` below, one
# run of `/host sh run` switches the bot console's code page from 950 to 65001
# (measured: 950 before, 65001 after, and **later calls without the prefix also
# emit UTF-8**), and every subsequent child process is affected—including the two
# `schtasks` queries that deliberately use `encoding="oem"` (`_process_control` and
# `install_autostart`). The symptoms of that breakage show up in **other
# features** and never point back here.
#
# The prefix and the flag are **a pair**: with only the flag and no prefix, U+FFFD
# is still 11 (the prefix does the work); with only the prefix and no flag, the
# output is right but the console gets polluted (the flag contains the side
# effect).
_PS_UTF8_PRELUDE = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
)

# The child gets **its own** hidden console, so the two setters above cannot touch
# ours. `CREATE_NO_WINDOW` only exists in Windows' `subprocess`, hence the guard;
# passing 0 is legal on other platforms (CPython only refuses when
# `creationflags != 0`).
# `DETACHED_PROCESS` cannot be used instead: measured, the output becomes an empty
# string.
_SHELL_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ANSI CSI / OSC escape sequences. PowerShell 7's table output is interspersed with
# colour codes (formatters like `Get-ChildItem`'s always do this), which on the chat
# platform are a mess of unreadable `[32;1m`. These are purely terminal
# presentation commands carrying no content, so `run_shell` strips them—which keeps
# the copy written to the log clean too. (Not switching to
# `$PSStyle.OutputRendering`: the built-in Windows 5.1 has no such variable, and
# the `powershell` fallback would error out.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


# The "current working directory" for command execution. Each `!sh` is a separate
# process, and the effect of `cd` does not carry over to the next one, so this
# module has to remember the working directory. It lives **in memory** rather than
# on disk: it is conversational context, not a cross-process contract, and going
# back to the project root after a bot restart is the expected behaviour.
_SHELL_CWD: Path = PROJECT_ROOT


def shell_cwd() -> Path:
    """The current working directory for `!sh`."""
    return _SHELL_CWD


def set_shell_cwd(raw: str | None) -> Path:
    """Change the working directory; `None` / an empty string goes back to the
    project root. Returns the path after the change."""
    global _SHELL_CWD  # pylint: disable=global-statement
    text = unquote_path(raw)
    if not text:
        _SHELL_CWD = PROJECT_ROOT
        return _SHELL_CWD
    # Relative paths are based on the **current** working directory, so that
    # `cd a` followed by `cd b` behaves intuitively (`resolve_host_path` is based
    # on the project root, which is meant for absolute positioning).
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    candidate = expanded if expanded.is_absolute() else _SHELL_CWD / expanded
    candidate = Path(os.path.normpath(str(candidate)))
    if not candidate.is_dir():
        raise GuiError("Folder not found.")
    _SHELL_CWD = candidate
    return _SHELL_CWD


def shell_argv(command: str, *, interactive: bool = False) -> list[str]:
    """Build the argv used to run a command.

    On Windows, PowerShell 7 (`pwsh`) is preferred, falling back to the built-in
    `powershell`; other platforms use `/bin/sh -c`. `-NoProfile` is always added:
    loading the user's profile slows down every call and may alter the
    environment.

    `-NonInteractive` is only added in **non**-interactive mode. Its purpose is
    "without a tty, do not sit at a prompt until the timeout", but it also makes
    `Read-Host` error out immediately—if the caller actually intends to feed input
    in (`job_send`), that flag would make the whole thing impossible.

    On Windows the user's command is prefixed with `_PS_UTF8_PRELUDE` (see the
    notes there): both output and input are switched to UTF-8, otherwise
    PowerShell's own Chinese goes through the console code page while native
    command output passes through unchanged, giving two encodings within one call.
    The prefix is **two statements**, and both are needed—setting only the output
    side still breaks Chinese fed in via `job_send`, and that breakage produces no
    error message at all. The prefix is neutral to the exit code (measured:
    `exit 3` gives rc=3 with or without the prefix).
    """
    if os.name == "nt":
        import shutil
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        argv = [exe, "-NoProfile"]
        if not interactive:
            argv.append("-NonInteractive")
        return [*argv, "-Command", _PS_UTF8_PRELUDE + command]
    return ["/bin/sh", "-c", command]


def _kill_tree(proc: subprocess.Popen) -> None:
    """After a timeout, clean up the whole child process tree.

    Killing only the direct child is not enough: under
    `powershell -Command "some.exe"` what is really running is a grandchild, and
    when the parent dies the grandchild lives on, so the timeout would effectively
    not take effect. psutil is a required dependency; when it is unavailable, fall
    back to killing only the direct child.
    """
    try:
        import psutil  # type: ignore
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
        _gone, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    try:
        proc.kill()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def run_shell(command: str, *, timeout: float = SHELL_DEFAULT_TIMEOUT_SEC,
              cwd: Path | None = None) -> dict[str, Any]:
    """Run one command line, returning `{rc, output, elapsed, timed_out}`
    (**synchronous, blocking**).

    `output` is the raw text of stdout + stderr combined, **not de-identified**—the
    caller is responsible for scrubbing it before it goes into the chat platform
    (`_scrub_external_report`). It is not done here because the copy written to
    the log needs the original text.

    The caller must check permissions itself: this function does not know who the
    owner is.
    """
    text = (command or "").strip()
    if not text:
        raise GuiError("Give the command to run.")
    limit = max(1.0, min(float(timeout), SHELL_MAX_TIMEOUT_SEC))
    cwd = cwd or shell_cwd()
    argv = shell_argv(text)
    started = time.monotonic()
    try:
        # nosec B603 — arbitrary command execution by design; the gate is at the
        # caller (owner-only). shell=False: argv goes straight to the interpreter,
        # without an extra layer of command-line parsing.
        proc = subprocess.Popen(  # nosec B603
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # The child gets its own hidden console, so the `SetConsoleOutputCP`
            # from `shell_argv`'s prefix cannot touch the bot's. Without it, a
            # single command run switches the bot console's code page from 950 to
            # 65001, and the two `encoding="oem"` `schtasks` queries break with
            # it—with the symptoms showing up in other features.
            creationflags=_SHELL_CREATIONFLAGS,
        )
    except OSError as error:
        raise GuiError("Could not start the command interpreter.") from error
    timed_out = False
    # Register it so `shell_stop_all()` has something to kill. Without this, a
    # mistyped `!sh` could only wait out its timeout once started (up to 15
    # minutes), with no way to intervene in between.
    with _job_lock():
        _SHELL_PROCS.add(proc)
    try:
        output, _ = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:  # pylint: disable=broad-except
            output = ""
    except Exception as error:  # pylint: disable=broad-except
        _kill_tree(proc)
        raise GuiError("An error occurred while running the command.") from error
    finally:
        with _job_lock():
            _SHELL_PROCS.discard(proc)
    output = strip_ansi(output or "")
    if len(output) > SHELL_MAX_OUTPUT_CHARS:
        output = output[:SHELL_MAX_OUTPUT_CHARS] + "\n… (output too long, truncated)"
    return {
        "rc": proc.returncode,
        "output": output,
        "elapsed": time.monotonic() - started,
        "timed_out": timed_out,
    }


# --------------------------------------------------------------------------
# Action recording
# --------------------------------------------------------------------------
# Installing the low-level keyboard/mouse hooks, recording "release" and the wheel,
# timestamping, and looking up key code → character for the keyboard layout are
# all in the library (`record` / `stop_record_timeline` / `char_table`). All that
# stays here is **converting events into this project's macro language**—that is
# this bot's own DSL and does not belong in the library.
#
# Why none of those three can be dropped (the library originally had none of them;
# they were added there before this moved over): without "release", a drag looks
# like just a click and a held modifier cannot be reconstructed; without the wheel,
# scrolling cannot be recorded at all; without timestamps, replay runs every step
# in one burst and a real interface cannot keep up.
#
# Security: the keyboard hook records **every character typed during the session,
# passwords included**. So recording is owner-only, has a hard time limit, and
# `!macro show` must de-identify before sending (what was recorded was not written
# by the user; it is what happened on the host).
RECORD_MAX_SEC = 300.0
RECORD_MIN_WAIT_SEC = 0.4       # below this gap no `wait` is inserted, so steps are not chopped up
RECORD_DOUBLE_CLICK_SEC = 0.4
RECORD_DRAG_MIN_PX = 6

_RECORDING = False
_RECORD_STARTED = 0.0
_RECORD_LAYOUT: int | None = None

# Modifier virtual-key code → the name used in macros. This table is vocabulary of
# **this project's macro language**, not system knowledge, so it stays here.
_MODIFIER_VK = {
    16: "shift", 160: "shift", 161: "shift",
    17: "ctrl", 162: "ctrl", 163: "ctrl",
    18: "alt", 164: "alt", 165: "alt",
    91: "win", 92: "win",
}

# The library's event verbs → the short names this module uses when converting to
# steps.
_RECORD_OPS = {"key_down": "kdown", "key_up": "kup",
               "mouse_down": "mdown", "mouse_up": "mup", "scroll": "wheel"}


def record_start() -> None:
    """Start recording. Raise `GuiError` if already recording."""
    global _RECORDING, _RECORD_STARTED, _RECORD_LAYOUT  # pylint: disable=global-statement
    if os.name != "nt":
        raise GuiError("Recording is only available on Windows.")
    if _RECORDING:
        raise GuiError("Already recording.")
    ac = load_ac()
    # Ask for the keyboard layout **when recording starts**: that is the one the
    # user is about to type with. Asking at stop time is too late—by then they are
    # back on the chat platform, the foreground window has changed, and the layout
    # may have changed with it.
    _RECORD_LAYOUT = ac.foreground_keyboard_layout()
    try:
        ac.record()
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] record start failed: {error!r}", file=sys.stderr)
        raise GuiError("Could not start recording (the keyboard/mouse listener could not be attached).") from error
    _RECORDING = True
    _RECORD_STARTED = time.monotonic()


def record_active() -> bool:
    return _RECORDING


def record_elapsed() -> float:
    return 0.0 if not _RECORDING else time.monotonic() - _RECORD_STARTED


def record_stop() -> list[dict[str, Any]]:
    """Stop recording, returning the event list this module converts into steps."""
    global _RECORDING  # pylint: disable=global-statement
    if not _RECORDING:
        raise GuiError("Not currently recording.")
    _RECORDING = False
    try:
        events = load_ac().stop_record_timeline()
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] record stop failed: {error!r}", file=sys.stderr)
        raise GuiError("Failed to stop recording.") from error
    return _from_timeline(events)


def _from_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The library's `delta_ms` events → events carrying an absolute time `t`.

    Converting to steps has to judge "how long the pause between two actions was"
    and "whether repeated clicks count as a double-click", which is easier to write
    with accumulated absolute time than with per-event gaps; the library returns
    gaps because that is the form replay needs.
    """
    out: list[dict[str, Any]] = []
    clock = 0.0
    for event in events:
        clock += float(event.get("delta_ms", 0)) / 1000.0
        kind = _RECORD_OPS.get(str(event.get("op", "")))
        if kind is None:
            continue
        item = {key: value for key, value in event.items()
                if key not in ("op", "delta_ms")}
        item["kind"] = kind
        item["t"] = clock
        out.append(item)
    return out


# Table name → readable alias (`return` → `enter`). Recorded steps are for people
# to read and edit, and the names used in the docs are easier to understand than
# the raw Win32 names; both pass validation.
_ALIAS_REVERSE: dict[str, str] = {}
for _friendly, _canonical in KEY_ALIASES.items():
    # The one declared first wins (`backspace` reads better than `bksp`), not the
    # shortest
    _ALIAS_REVERSE.setdefault(_canonical, _friendly)
del _friendly, _canonical


def _vk_to_name(vk: int) -> str | None:
    """Virtual-key code → the key name used in macros; None if the table has no
    entry.

    Reverse lookup hits name collisions (the mouse-event constants share one table
    with the keyboard: vk 32 is both `space` and `middledown`, and vk 84 is both
    `t` and `T`), so there has to be an explicit order of preference, otherwise the
    same recording would produce different text on different machines.

    Candidate names come from both the low-level table and `_EXTRA_KEY_CODES`
    (what `parse_key_name` recognises is exactly the union of the two), compared
    with the same order of preference—so when vk 183's `LAUNCH_APP2` (uppercase,
    which `parse_key_name` cannot write) and the extra table's `launch_app2` are
    both present, the lowercase one is always picked. The extra table belongs to
    this project, so it can be looked up even when the low level cannot load.

    `_LIBRARY_NAME_OVERRIDES` has two rules, both about "that entry in the
    low-level table is wrong":

    * an overridden name is **not used for reverse lookup of its wrong key code in
      the low-level table**—vk 0x80 (F17) must not be recorded as `down`, or on
      replay `down` would become 0x28 and the recorded F17 would turn into an arrow
      key;
    * an overriding name **wins explicitly** (not by luck of length), so vk 0x28 is
      recorded as `down` rather than the low level's `vk_down`. The override table
      only holds names an ordinary person would type anyway.
    """
    if vk in _MODIFIER_VK:
        return _MODIFIER_VK[vk]
    try:
        table = getattr(load_ac(), "keyboard_keys_table", None)
    except GuiError:
        table = None
    if not isinstance(table, dict):
        table = {}
    names = [name for name, code in table.items()
             if code == vk and name not in _LIBRARY_NAME_OVERRIDES]
    names += [name for name, code in _EXTRA_KEY_CODES.items() if code == vk]
    overriding = [name for name, code in _LIBRARY_NAME_OVERRIDES.items()
                  if code == vk]
    if overriding:
        names = overriding
    if not names:
        return None
    # Order of preference: lowercase first → shorter first → lexicographic
    # (guaranteeing consistency across machines)
    best = min(names, key=lambda n: (n != n.lower(), len(n), n))
    return _ALIAS_REVERSE.get(best, best)


def _record_char_table() -> dict[int, tuple[str, str]]:
    """The "key → character" table for the keyboard layout in use while
    recording.

    Punctuation virtual-key codes print different characters on different
    layouts, and hardcoding a US table would make every punctuation mark recorded
    on a non-US layout wrong. Both the lookup and the fallback are in the library.
    """
    try:
        return load_ac().char_table(_RECORD_LAYOUT)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] char table unavailable: {error!r}", file=sys.stderr)
        return {}


class RecordedMacro(NamedTuple):
    """The result of converting a recording into steps, together with **how much
    was not saved**.

    * `steps` — steps that can be saved directly (this is what `record_to_steps`
      returns);
    * `truncated` — the number of **actions** not saved because they exceeded
      `MACRO_MAX_STEPS` (excluding `wait`: waits after the cut-off lose their
      meaning along with the last action anyway, and counting them would only make
      the number look bigger than the real loss);
    * `unrecordable` — the number of steps that were converted but cannot be
      replayed, and were skipped (keys that cannot be named, steps that fail
      validation). This number is **independent of the step cap**: steps after the
      cut-off are still validated one by one and classified, so the same recording
      skips the same number regardless of the cap.

    Both numbers exist for reporting: a recording can run up to `RECORD_MAX_SEC`,
    and a hundred clicks (each one after a pause is two steps, `wait` + `click`)
    already reach the cap, while the reply used to say only "saved as a macro (200
    steps)", so the user would never learn about the part dropped after that.
    """

    steps: list[str]
    truncated: int
    unrecordable: int


def record_to_steps(events: list[dict[str, Any]], *,
                    min_wait: float = RECORD_MIN_WAIT_SEC,
                    char_table: dict[int, tuple[str, str]] | None = None
                    ) -> list[str]:
    """Convert recorded events into macro steps (steps only; to learn how much was
    dropped, use `convert_recording`)."""
    return convert_recording(events, min_wait=min_wait,
                             char_table=char_table).steps


def convert_recording(events: list[dict[str, Any]], *,
                      min_wait: float = RECORD_MIN_WAIT_SEC,
                      char_table: dict[int, tuple[str, str]] | None = None
                      ) -> RecordedMacro:
    """Convert recorded events into macro steps, returning a `RecordedMacro`.

    The conversion rules are deliberately conservative—better an extra step that
    is easy to understand than a wrong guess:

    * press and release positions differing by more than `RECORD_DRAG_MIN_PX` →
      `drag`, otherwise `click`;
    * two clicks at the same point with **the same mouse button** within
      `RECORD_DOUBLE_CLICK_SEC` → merged into `dclick` (a right click followed by a
      left click is two different clicks, not one left double-click);
    * consecutive wheel events are merged into one `scroll`; a group that sums to 0
      (three notches down then three up) **produces no step at all**, not even its
      own `wait`—that time is merged into the wait before the next action;
    * printable-character keys (with no ctrl / alt / win held) are merged into one
      `type`, and other keys come out as `hotkey` (with the modifiers held at the
      time);
    * a gap between events longer than `min_wait` adds a `wait`—**this is what
      decides whether replay can succeed**; without it every step is sent in one
      burst and the screen simply cannot keep up;
    * at most `MACRO_MAX_STEPS` steps are saved; actions after that are not saved
      but **are counted in `truncated`**; steps that cannot be replayed are skipped
      and counted in `unrecordable` (both must be reported to the user, see
      `RecordedMacro`).

    Key codes are converted into characters using **the keyboard layout in use while
    recording** (captured by `record_start`), falling back to the US table only when
    it cannot be asked—punctuation virtual-key codes print different characters on
    different layouts, and hardcoding one table would make every punctuation mark
    recorded on a non-US layout wrong. The lookup itself is in the library.

    `type` text goes through `escape_macro_text` (`$` → `$$`), so a recorded `$100`
    still replays as `$100` rather than being taken as an argument. This is the only
    kind of step that carries recorded text: `hotkey` key names can only be letters,
    digits and underscores.

    **A known, deliberately unfixed distortion: whitespace.** `type`'s arguments are
    split on whitespace and rejoined with single spaces on replay, and the text is
    also `.strip()`ped here when merging, so runs of whitespace collapse to one and
    leading/trailing whitespace disappears. It is not fixed because fixing it would
    mean changing `type`'s whitespace semantics, which would silently change every
    existing hand-written macro.
    """
    printable = char_table if char_table is not None else _record_char_table()
    steps: list[str] = []
    pending_text: list[str] = []
    held: set[str] = set()
    down: dict[str, dict[str, Any]] = {}
    wheel: dict[str, Any] | None = None
    last_t: float | None = None
    # The previous click: `(x, y, release time, which button)`. The button has to
    # be compared too, otherwise a right click followed by a left click would be
    # merged into one left double-click (and the right click would simply vanish).
    last_click: tuple[int, int, float, str] | None = None

    def _flush_text() -> None:
        if pending_text:
            text = "".join(pending_text).strip()
            pending_text.clear()
            if text:
                # What was recorded is text really typed on the host, so any `$` in
                # it must be escaped as `$$`, otherwise on replay `$1` would be
                # taken as an argument (`$100` → `00`).
                steps.append(f"type {escape_macro_text(text)}")

    def _flush_wheel() -> None:
        nonlocal wheel
        if wheel is not None:
            # The library already returns "notches" (the raw value divided by 120
            # per notch), so no further conversion is needed.
            #
            # A group that sums to 0 (scrolling down and back up to where it was)
            # **produces nothing**. This used to read `int(...) or 1`, so a group of
            # scrolls that cancelled out was recorded as one notch up—an action that
            # never happened. `scroll 0` passes validation anyway; that `or 1` was
            # not there for validation.
            #
            # This group's `wait` is also only added here (`_gap` was deferred from
            # the start to this point), so a dropped group leaves no wait of its
            # own; `last_t` is untouched too, so that time naturally merges into the
            # `wait` before the next action and the total replay time is not
            # shortened.
            notches = int(wheel["delta"])
            if notches:
                _gap(wheel["start"])
                steps.append(f"scroll {notches}")
            wheel = None

    def _gap(now: float) -> None:
        nonlocal last_t
        if last_t is not None and now - last_t >= min_wait:
            steps.append(f"wait {min(round(now - last_t, 1), MACRO_MAX_WAIT_SEC):g}")
        last_t = now

    for event in events:
        kind = event.get("kind")
        now = float(event.get("t", 0.0))

        if kind == "wheel":
            # Consecutive scrolling merges into one step; it is only emitted once
            # interrupted
            if wheel is not None and now - wheel["t"] < 0.3:
                wheel["delta"] += event.get("delta", 0)
                wheel["t"] = now
                continue
            _flush_wheel()
            _flush_text()
            wheel = {"delta": event.get("delta", 0), "t": now, "start": now}
            continue
        _flush_wheel()

        if kind == "mdown":
            down[event.get("button", "left")] = event
            continue

        if kind == "mup":
            button = event.get("button", "left")
            start = down.pop(button, None)
            if start is None:
                continue
            _flush_text()
            _gap(float(start.get("t", now)))
            x1, y1 = int(start.get("x", 0)), int(start.get("y", 0))
            x2, y2 = int(event.get("x", x1)), int(event.get("y", y1))
            suffix = "" if button == "left" else f" {button}"
            if abs(x2 - x1) >= RECORD_DRAG_MIN_PX or abs(y2 - y1) >= RECORD_DRAG_MIN_PX:
                steps.append(f"drag {x1} {y1} {x2} {y2}{suffix}")
                last_click = None
            else:
                if (last_click is not None and steps
                        and last_click[3] == button
                        and abs(last_click[0] - x2) < RECORD_DRAG_MIN_PX
                        and abs(last_click[1] - y2) < RECORD_DRAG_MIN_PX
                        and now - last_click[2] <= RECORD_DOUBLE_CLICK_SEC
                        and steps[-1].startswith("click ")):
                    steps[-1] = f"dclick {x2} {y2}{suffix}"
                    last_click = None
                else:
                    steps.append(f"click {x2} {y2}{suffix}")
                    last_click = (x2, y2, now, button)
            last_t = now
            continue

        if kind == "kdown":
            vk = int(event.get("vk", 0))
            name = _vk_to_name(vk)
            if vk in _MODIFIER_VK:
                held.add(_MODIFIER_VK[vk])
                continue
            hard = held - {"shift"}
            character = printable.get(vk)
            if not hard and character:
                if not pending_text:
                    _gap(now)
                pending_text.append(character[1] if "shift" in held else character[0])
                last_t = now
                continue
            _flush_text()
            _gap(now)
            combo = "+".join([*sorted(held), name or str(vk)])
            steps.append(f"hotkey {combo}" if name else f"# unknown key vk={vk}")
            last_t = now
            continue

        if kind == "kup":
            vk = int(event.get("vk", 0))
            if vk in _MODIFIER_VK:
                held.discard(_MODIFIER_VK[vk])

    _flush_wheel()
    _flush_text()
    # Validate once before saving: what was recorded is what happened on the host,
    # not something the user wrote, and one invalid step mixed in would get the
    # whole macro rejected by `load_macro` (rather than just that step skipped).
    #
    # The part over the step cap **does not simply break**: the remaining steps are
    # still validated one by one, with replayable actions counted in `truncated`
    # and unreplayable ones in `unrecordable`. It used to break at the cap, so a
    # three-hundred-second recording saved only the first two hundred steps while
    # the reply just said "saved as a macro (200 steps)".
    valid: list[str] = []
    truncated = 0
    unrecordable = 0
    for step in steps:
        if step.startswith("#"):
            # A key that cannot be named (see `_EXTRA_KEY_CODES`). This used to be
            # **completely silent**: the step count the recording reported was
            # simply one short, with nothing even in the log; the content is only a
            # key-code number.
            print(f"[gui] dropped unrecordable step {step!r}", file=sys.stderr)
            unrecordable += 1
            continue
        # A leading `wait` is always redundant: nothing has been done yet, so there
        # is nothing to wait for. The "moving your hand to the mouse" gap at the
        # start of a recording produces it, and keeping it only makes replay a beat
        # slower for no reason.
        if not valid and step.startswith("wait "):
            continue
        try:
            validate_macro_step(step)
        except GuiError:
            print(f"[gui] dropped unrecordable step {step!r}", file=sys.stderr)
            unrecordable += 1
            continue
        if len(valid) >= MACRO_MAX_STEPS:
            if not step.startswith("wait "):
                truncated += 1
            continue
        valid.append(step)
    while valid and valid[-1].startswith("wait "):
        valid.pop()      # the same goes for a trailing `wait`: no steps follow it
    return RecordedMacro(valid, truncated, unrecordable)


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------
# `run_shell` **blocks until the command finishes**, capped at 900 seconds. Things
# like installs, builds and downloads that routinely take twenty minutes therefore
# cannot be done through chat—not slow, but simply unable to finish.
#
# This starts a process without waiting for it, with a reader thread collecting its
# output into memory, retrieved later with `job_log`. The output **stays in memory
# and never touches disk**: it is temporary conversational state, not a
# cross-process contract, and writing it to disk would need an extra cleanup and
# file-naming policy. The price is that it is gone after a bot restart (the process
# itself is also reaped along with its parent), which the docs state clearly.
JOB_MAX_KEPT = 20
JOB_LOG_MAX_LINES = 4000
JOB_LOG_LINE_MAX_CHARS = 2000

_JOBS: dict[int, dict[str, Any]] = {}
_JOB_NEXT_ID = 1
_JOB_LOCK: Any = None


def _job_lock():
    """The reader threads and callers touch `_JOBS` concurrently, so it needs a
    lock. Created lazily so importing does not pay the cost."""
    global _JOB_LOCK  # pylint: disable=global-statement
    if _JOB_LOCK is None:
        import threading
        _JOB_LOCK = threading.Lock()
    return _JOB_LOCK


def _close_job_streams(job: dict[str, Any]) -> None:
    """Explicitly close the job's pipes. Never raises.

    `Popen`'s stdout/stdin are `TextIOWrapper`s; left open, they are only
    collected by `__del__` once the last reference goes away—measured (20 jobs,
    Windows handle count), they do currently drop to zero the moment the `_JOBS`
    entry is discarded, so this is **not** a handle leak; but that relies on
    CPython's reference counting, and any reference cycle (an exception traceback
    holding a frame is enough) would defer the release to GC. Since the process
    runs for days, closing explicitly is cheap and needs no gamble.

    A side effect is that a `-W always::ResourceWarning` sweep of the whole suite
    comes out clean, which is what makes that sweep usable—only a guard that barks
    gets listened to.
    """
    proc = job.get("proc") if isinstance(job, dict) else None
    for name in ("stdout", "stderr", "stdin"):
        # `getattr` is inside the try too: its default only catches
        # `AttributeError`, and an attribute whose evaluation blows up (any
        # non-Popen stand-in might) would escape directly, and since this function
        # runs on the `finally` wrap-up path, escaping would mask the real error.
        try:
            stream = getattr(proc, name, None)
            if stream is not None:
                stream.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass


def _job_reader(job: dict[str, Any]) -> None:
    """Collect the child's output line by line into the job's ring buffer until it
    ends."""
    proc = job["proc"]
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            clean = strip_ansi(line.rstrip("\n"))[:JOB_LOG_LINE_MAX_CHARS]
            with _job_lock():
                job["lines"].append(clean)
                job["total_lines"] += 1
                # Keep only the most recent stretch: a chatty build can emit
                # hundreds of thousands of lines, and keeping them all would eat
                # the bot's memory. The number of dropped lines is recorded
                # separately, so nobody mistakes what they see for the whole thing.
                overflow = len(job["lines"]) - JOB_LOG_MAX_LINES
                if overflow > 0:
                    del job["lines"][:overflow]
                    job["dropped"] += overflow
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] job {job['id']} reader failed: {error!r}", file=sys.stderr)
    finally:
        try:
            proc.wait()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        with _job_lock():
            job["rc"] = proc.returncode
            job["finished"] = time.time()
        # The process has ended (`wait` returned) and nobody will use the pipes
        # again—close them explicitly rather than leaving it to the timing of
        # reference counting. `job_send` blocks finished jobs first, and
        # `job_close_input` is a no-op on a closed stream, so neither user-facing
        # entry point is affected.
        _close_job_streams(job)


def job_start(command: str, *, cwd: Path | None = None,
              interactive: bool = False) -> int:
    """Start a background job, returning its job number. **Does not wait for it to
    finish.**

    Only `interactive=True` connects stdin as a pipe (to feed input later with
    `job_send`). **The default is `DEVNULL`**, because that makes a program reading
    stdin get EOF immediately and finish normally; with a pipe connected but nobody
    writing, the same program would sit there until the timeout. Whoever needs to
    answer interactive prompts asks for it explicitly; everything else keeps the
    "will not mysteriously hang" default.
    """
    text = (command or "").strip()
    if not text:
        raise GuiError("Give the command to run.")
    global _JOB_NEXT_ID  # pylint: disable=global-statement
    _job_prune()
    argv = shell_argv(text, interactive=interactive)
    try:
        # nosec B603 — arbitrary command execution by design; the gate is at the
        # caller (owner-only).
        proc = subprocess.Popen(  # nosec B603
            argv,
            cwd=str(cwd or shell_cwd()),
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Same as `run_shell`: contain the console side effect of
            # `shell_argv`'s prefix. This stop in particular must not miss it—an
            # interactive job is the only path that **writes** stdin, and the
            # prefix's `InputEncoding` line was added for exactly that.
            creationflags=_SHELL_CREATIONFLAGS,
        )
    except OSError as error:
        raise GuiError("Could not start the command interpreter.") from error
    with _job_lock():
        job_id = _JOB_NEXT_ID
        _JOB_NEXT_ID += 1
        job = {
            "id": job_id,
            "command": text,
            "proc": proc,
            "started": time.time(),
            "finished": None,
            "rc": None,
            "lines": [],
            "total_lines": 0,
            "dropped": 0,
            "stopped": False,
            "interactive": bool(interactive),
        }
        _JOBS[job_id] = job
    import threading
    threading.Thread(target=_job_reader, args=(job,), daemon=True).start()
    return job_id


def _job_prune() -> None:
    """Keep the most recent `JOB_MAX_KEPT` job records, discarding only finished
    ones."""
    with _job_lock():
        done = sorted((j for j in _JOBS.values() if j["finished"] is not None),
                      key=lambda j: j["finished"])
        while len(_JOBS) > JOB_MAX_KEPT and done:
            _JOBS.pop(done.pop(0)["id"], None)


def job_list() -> list[dict[str, Any]]:
    """A status summary of every job (without output content), newest first."""
    with _job_lock():
        rows = list(_JOBS.values())
    now = time.time()
    out = []
    for job in sorted(rows, key=lambda j: -j["started"]):
        out.append({
            "id": job["id"],
            "command": job["command"],
            "running": job["finished"] is None,
            "rc": job["rc"],
            "stopped": job["stopped"],
            "elapsed": (job["finished"] or now) - job["started"],
            "lines": job["total_lines"],
        })
    return out


def _job_get(job_id: int) -> dict[str, Any]:
    with _job_lock():
        job = _JOBS.get(int(job_id))
    if job is None:
        raise GuiError("No job with that number was found.")
    return job


def job_log(job_id: int, lines: int = 40) -> dict[str, Any]:
    """A job's most recent `lines` lines of output. The output is **not
    de-identified**; that is the caller's job."""
    job = _job_get(job_id)
    count = max(1, min(int(lines), JOB_LOG_MAX_LINES))
    with _job_lock():
        tail = job["lines"][-count:]
        return {
            "id": job["id"],
            "running": job["finished"] is None,
            "rc": job["rc"],
            "stopped": job["stopped"],
            "elapsed": (job["finished"] or time.time()) - job["started"],
            "text": "\n".join(tail),
            "shown": len(tail),
            "total": job["total_lines"],
            "dropped": job["dropped"],
        }


def shell_stop_all() -> int:
    """Kill every running `run_shell` child process tree, returning how many were
    killed.

    It is deliberately "all" rather than picking one: `!sh` is synchronous and
    blocking, so in practice there are never many running at once, and making the
    user look up a number before they can kill one makes no sense at the moment of
    "the command will not finish and I want to stop it".
    """
    with _job_lock():
        procs = list(_SHELL_PROCS)
    for proc in procs:
        _kill_tree(proc)
    return len(procs)


def job_send(job_id: int, text: str, *, newline: bool = True) -> None:
    """Feed one line of input to an interactive job (the kind that answers an
    installer's prompts)."""
    job = _job_get(job_id)
    if not job.get("interactive"):
        raise GuiError("This job was not started with interactive input; restart it with `--stdin`.")
    if job["finished"] is not None:
        raise GuiError("This job has already finished.")
    stream = job["proc"].stdin
    if stream is None:
        raise GuiError("This job has no writable input.")
    try:
        stream.write(text + ("\n" if newline else ""))
        stream.flush()
    except (OSError, ValueError) as error:
        raise GuiError("Failed to write to the job's input (it may no longer be reading).") from error


def job_close_input(job_id: int) -> None:
    """Close the job's input end, so the other side reads EOF."""
    job = _job_get(job_id)
    stream = job["proc"].stdin
    if stream is None:
        raise GuiError("This job has no writable input.")
    try:
        stream.close()
    except (OSError, ValueError) as error:
        raise GuiError("Failed to close the job's input.") from error


def job_stop(job_id: int) -> bool:
    """Kill the job's whole process tree. Returns False if it has already
    finished."""
    job = _job_get(job_id)
    if job["finished"] is not None:
        return False
    job["stopped"] = True
    _kill_tree(job["proc"])
    return True


def job_clear() -> int:
    """Clear every finished job record, returning how many were cleared."""
    with _job_lock():
        done = [k for k, j in _JOBS.items() if j["finished"] is not None]
        for key in done:
            job = _JOBS.pop(key, None)
            if job is not None:
                # Insurance: the reader thread already closed them when it ended
                # normally, but if it never started (`Thread.start` failed) nobody
                # did. Closing a second time is a no-op.
                _close_job_streams(job)
    return len(done)


def job_stop_all() -> int:
    """Kill every job still running. Used when the bot shuts down—otherwise
    orphan processes would be left on the host."""
    stopped = 0
    for row in job_list():
        if row["running"]:
            try:
                if job_stop(row["id"]):
                    stopped += 1
            except GuiError:  # pragma: no cover
                continue
    return stopped


# --------------------------------------------------------------------------
# Macro replay (synchronous version, for tests and non-async callers)
# --------------------------------------------------------------------------
def substitute_macro_args(step: str, args: list[str]) -> str:
    """Replace the argument markers in a step with the values given at call time.

    Parameterisation lets one macro be reused on different targets
    (`/macro run open_url <url>`), without copying it just to change one word.
    Syntax:

    * `$1`..`$9` → the Nth argument; missing ones become an empty string. Only
      **one digit** is read, so `$10` is "the first argument followed by a `0`".
    * `$$` → a literal `$`. Without this rule, "`$` followed by a digit" could not
      be written in a macro at all (`type $100` would type `00`), and recorded
      input would be silently altered too.
    * Any other `$` (a trailing `$`, `$a`, `$0`) is left as-is—just as before `$$`
      was added.

    **A single pass only**: substituted argument values are not scanned again, so a
    `$1` / `$$` inside an argument is sent as-is, and does not become another
    argument or get folded into one `$`. Matching runs left to right, so `$$$1` is
    "`$` + the first argument". The reverse direction is `escape_macro_text`.
    """
    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "$":
            return "$"
        index = int(token) - 1
        return args[index] if 0 <= index < len(args) else ""

    return MACRO_ARG_RE.sub(_replace, step)


def escape_macro_text(text: str) -> str:
    """Write a piece of text that must be sent verbatim as a macro step: `$` →
    `$$`.

    `substitute_macro_args(escape_macro_text(t), args) == t` holds for any `t`—every
    `$` becomes a pair, so when matching left to right no lone `$` is left to eat
    the digit after it. Recording (`record_to_steps`) uses this when it produces
    `type` steps.
    """
    return text.replace("$", "$$")


def check_macro_program(steps: list[str], args: list[str] | None = None, *,
                        name: str | None = None, depth: int = 0) -> None:
    """Validate the whole program **before** replay: every step after argument
    substitution, block balance, and every `call`.

    `run_macro_program` substitutes `$N` and validates as it runs, so with
    `['click 10 10', 'type $1']` and no arguments, the click has already really
    happened before the second line reveals that `type` has no argument; the same
    goes for a `call` to a macro that does not exist and recursion beyond
    `MACRO_MAX_CALL_DEPTH`—the outer steps are already done on the real desktop and
    cannot be taken back (`macro_block_map` states the same principle). This walks
    the whole call tree before any action.

    * Every step is validated, **whether or not execution would reach it**: a step
      that breaks when reached is broken.
    * Every `call` is followed: the called macro is loaded and checked with the
      substituted arguments, and the depth limit raises the same sentence as the
      execution side. So a conditional self-call (`if_text Error` / `call` to itself)
      is blocked too: fully expanded, it always exceeds the depth limit.
    * Memoised on `(macro name, args)`, so the same callee is validated only once;
      the total is also bounded by `MACRO_MAX_CHECKED` (memoisation itself is not a
      bound, see that constant).
    * A failure raises `GuiError`, whose message says which macro and which line,
      and the whole sentence is a generic one written by this module (macro names
      have passed `MACRO_NAME_RE`, and argument values never appear in the
      message).

    `name` is only used so the message can point at which macro it is; one that
    does not match `MACRO_NAME_RE` is not printed. Creating a schedule / watch also
    calls this, so bad arguments are rejected **at creation time**.
    """
    label = name if name and MACRO_NAME_RE.match(name) else None
    _check_macro_level(list(steps), list(args or []), label, depth,
                       memo={}, loaded={}, counter=[0])


def _check_macro_level(steps: list[str], args: list[str], name: str | None,
                       depth: int, *,
                       memo: dict[tuple[str, tuple[str, ...]], int],
                       loaded: dict[str, list[str]],
                       counter: list[int]) -> int:
    """One level of `check_macro_program`; returns how many levels of `call` lie
    below this one (the height).

    Memoisation stores the **height** rather than "validated": the same
    `(macro, args)` may validate fine at a shallow spot but not at a deep one—a
    call tree of height 2 at depth 1 is fine, while the same tree hung at depth 2
    exceeds the limit. The height does not depend on where it is called from, so a
    hit only needs one comparison of `depth + 1 + height`. Only **successful**
    subtrees enter the memo, so a call cycle always expands all the way to the
    depth limit and then fails.
    """
    if depth > MACRO_MAX_CALL_DEPTH:
        raise GuiError(f"The macro call depth exceeds the limit ({MACRO_MAX_CALL_DEPTH} levels).")
    where = f"Macro `{name}`: " if name else ""
    try:
        macro_block_map(steps)
    except GuiError as error:
        if not name:
            raise
        raise GuiError(f"Macro `{name}`: {error}") from error
    height = 0
    for index, step in enumerate(steps, start=1):
        counter[0] += 1
        if counter[0] > MACRO_MAX_CHECKED:
            raise GuiError(
                f"After expansion, the macro has more steps to check than the limit ({MACRO_MAX_CHECKED} steps); "
                "reduce the depth or branching of `call`.")
        raw = substitute_macro_args(step, args)
        try:
            verb, step_args = validate_macro_step(raw)
        except GuiError as error:
            raise GuiError(f"{where}Line {index}: {error}") from error
        if verb != "call":
            continue
        callee = step_args[0]          # already passed `macro_path`'s name check
        key = (callee, tuple(step_args[1:]))
        below = memo.get(key)
        if below is not None:
            if depth + 1 + below > MACRO_MAX_CALL_DEPTH:
                raise GuiError(
                    f"{where}Line {index}: The macro call depth exceeds the limit "
                    f"({MACRO_MAX_CALL_DEPTH} levels).")
        else:
            callee_steps = loaded.get(callee)
            if callee_steps is None:
                try:
                    callee_steps = load_macro(callee)["steps"]
                except GuiError as error:
                    raise GuiError(
                        f"{where}Line {index}: Macro `{callee}`: {error}") from error
                loaded[callee] = callee_steps
            try:
                below = _check_macro_level(
                    callee_steps, list(step_args[1:]), callee, depth + 1,
                    memo=memo, loaded=loaded, counter=counter)
            except GuiError as error:
                raise GuiError(f"{where}Line {index}: {error}") from error
            memo[key] = below
        height = max(height, below + 1)
    return height


def run_macro_program(steps: list[str], *, args: list[str] | None = None,
                      on_step: Callable[[int, str, str], None] | None = None,
                      should_abort: Callable[[], bool] | None = None,
                      depth: int = 0,
                      budget: list[int] | None = None) -> list[str]:
    """Run a macro program (including `repeat` / `if_…` / `call`), returning a
    description of each step.

    An interpreter rather than a line-by-line loop: once there are blocks, a
    program counter and a control stack are needed. The key design points—

    * **The number of executed steps has a total cap** (`MACRO_MAX_EXECUTED`).
      Nested `repeat` can demand a million steps in just four lines, which a cap on
      source lines cannot stop at all; without this cap, one slip of a number
      would occupy the desktop for tens of minutes with no way to abort.
    * **`call` has a depth limit**; two macros calling each other is an endless
      recursion that is very natural to write.
    * **`should_abort()` is checked at every step**, which is what lets
      `!macro stop` stop it.
    * **The whole program is validated before the first action**
      (`check_macro_program`, done once at the outermost level only): steps that
      only break after argument substitution, a `call` to a macro that does not
      exist, recursion beyond the depth limit—all must be reported before the mouse
      moves, not by stopping halfway. The per-step substitution and validation in
      the loop **are kept anyway**—while a long macro is running, the called macro
      file may still be changed.

    When it ends (including failure / abort), it releases the keys **this macro
    itself held down**; keys the user was already holding before the run are left
    alone, as that is state they left on purpose.
    """
    if depth > MACRO_MAX_CALL_DEPTH:
        raise GuiError(f"The macro call depth exceeds the limit ({MACRO_MAX_CALL_DEPTH} levels).")
    if depth == 0:
        check_macro_program(steps, args)
    blocks = macro_block_map(steps)
    counters = budget if budget is not None else [0]
    snapshot = held_snapshot() if depth == 0 else None
    done: list[str] = []
    # Control stack: `("loop", start, end, remaining count)` or `("if", end)`
    stack: list[tuple] = []
    pointer = 0
    try:
        while pointer < len(steps):
            if should_abort is not None and should_abort():
                break
            counters[0] += 1
            if counters[0] > MACRO_MAX_EXECUTED:
                raise GuiError(
                    f"The number of executed steps exceeded the limit ({MACRO_MAX_EXECUTED} steps), so it was aborted; "
                    "check the `repeat` counts.")
            raw = substitute_macro_args(steps[pointer], args or [])
            verb, step_args = validate_macro_step(raw)

            if verb == "stop":
                break
            if verb == "repeat":
                count = int(step_args[0])
                _else, end = blocks[pointer]
                if count <= 0:
                    pointer = end + 1
                    continue
                stack.append(("loop", pointer, end, count - 1))
                pointer += 1
                continue
            if verb in MACRO_CONDITIONS:
                else_at, end = blocks[pointer]
                stack.append(("if", end))
                if eval_macro_condition(verb, step_args):
                    pointer += 1
                else:
                    pointer = (else_at + 1) if else_at is not None else end
                continue
            if verb == "else":
                # Reaching here means the if's true branch has finished; skip the
                # else branch
                frame = stack[-1] if stack else None
                pointer = (frame[1] if frame and frame[0] == "if"
                           else pointer + 1)
                continue
            if verb == "end":
                frame = stack.pop() if stack else None
                if frame and frame[0] == "loop" and frame[3] > 0:
                    stack.append(("loop", frame[1], frame[2], frame[3] - 1))
                    pointer = frame[1] + 1
                    continue
                pointer += 1
                continue
            if verb == "call":
                data = load_macro(step_args[0])
                nested = run_macro_program(
                    data["steps"], args=step_args[1:], on_step=on_step,
                    should_abort=should_abort, depth=depth + 1, budget=counters)
                done.extend(nested)
                detail = f"called macro `{step_args[0]}` ({len(nested)} steps)"
            else:
                try:
                    detail = run_macro_step(raw, should_abort=should_abort)
                except GuiAborted:
                    # A wait step being aborted is not a failure; it is the same
                    # thing as "being stopped between steps".
                    break

            done.append(detail)
            if on_step is not None:
                on_step(len(done), raw, detail)
            pointer += 1
    finally:
        if snapshot is not None:
            release_added_since(snapshot)
    return done


def run_macro(steps: list[str], *,
              on_step: Callable[[int, str, str], None] | None = None,
              should_abort: Callable[[], bool] | None = None) -> list[str]:
    """The argument-less version of `run_macro_program` (for tests and non-async
    callers)."""
    return run_macro_program(steps, on_step=on_step, should_abort=should_abort)
