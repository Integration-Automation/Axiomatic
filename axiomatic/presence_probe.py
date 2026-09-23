"""
presence_probe.py — 從本機 OS 抓「正在玩什麼遊戲 / 正在聽什麼音樂」，
讓 Discord bot 即使在目標使用者隱身時也能鏡像出有意義的狀態。

只回報遊戲跟音樂兩種 activity；前景視窗只在嚴格的音樂標題 pattern
（例如 `<曲名> - <歌手> - YouTube Music`）會被當訊號用，其他一般視窗
標題不會被當成 activity。

公開介面：
    probe_signals_async()
        → 一次抓齊所有 presence 訊號（game / music_strict / music_any /
          claude），再交給 bot_activity_from_signals /
          rpc_activity_from_signals 兩個純函式各自組裝成 activity。

設定檔：
    presence_games.json — repo 根目錄，內容為 {"<exe-檔名 lowercase>": "<顯示名稱>"}。
    例：{"skyrimse.exe": "Skyrim SE", "endfield.exe": "Arknights: Endfield"}
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GAMES_FILE = PROJECT_ROOT / "presence_games.json"
MUSIC_FILE = PROJECT_ROOT / "presence_music.json"
# 與 discord_rpc.py 共讀同一個檔，但彼此不互相 import（兩個被動 reader）：
# 這裡只取 `claude` 區塊決定「要不要偵測 Claude Code、偵測哪些 exe」，
# discord_rpc 取 client_id / kinds 等「怎麼顯示」。
RPC_CONFIG_FILE = PROJECT_ROOT / "presence_rpc.json"
_DEFAULT_CLAUDE_PROCESSES = ("claude.exe",)

# 「這個鍵根本沒寫」與「寫了但寫錯（含寫成 null）」要分得開：前者是正常情況、
# 不該有任何輸出，後者要留下痕跡。`dict.get(key)` 回 None 時兩者無法區分。
_UNSET = object()

# 三個設定檔都是使用者手改的，用 utf-8-sig 讀：沒有 BOM 時與 utf-8 完全
# 等價，有 BOM（Windows 上用 PowerShell `Set-Content -Encoding UTF8` 或舊版
# 編輯器存檔就會產生）時也不會整份 JSON 解析失敗、靜默退回預設值。
_CONFIG_ENCODING = "utf-8-sig"

# 警告去重搬到 `_warn_dedup`（`CLAUDE.md` 允許的被動共用模組：純標準函式庫、
# 不 import 專案裡的任何東西）。原本這裡有一份逐字相同的六行實作，`_batch_config`
# 的註解寫著「若出現第三份就該提成共用模組」——第三份出現了。別名成 `_warn_once`
# 是為了讓既有呼叫端一個字都不用改。
# **雙形狀匯入，不要收回成單獨一行裸名。** 裸名只在「`axiomatic/` 自己在
# `sys.path` 上」時成立——跑 `webrunner_*.py` / `discord_bot.py` 這種腳本時
# `sys.path[0]` 正好就是它們所在的那個目錄，所以本機怎麼跑都對。但
# `start_webrunner.py` 住在 repo root、**刻意**走套件路徑
# `from axiomatic._bot_config import ...`（理由見它自己的註解：裸名版本只有執行期
# 才成立，靜態分析器看不到 `sys.path.insert`）。走那條路徑時 `axiomatic/` 不在
# `sys.path` 上，裸名就是 `ModuleNotFoundError`，而且是在啟動器 import 期炸掉——
# 整支 webrunner 起不來，rc=1，重啟幾次都一樣。2026-09-12 實際發生過。
# `_external_apis` 從一開始就是這個形狀。
try:
    from _warn_dedup import warn_once as _warn_once   # noqa: E402
except ImportError:  # 套件路徑（`from axiomatic import presence_probe`）
    from axiomatic._warn_dedup import warn_once as _warn_once  # type: ignore  # noqa: E402


def _warn_unknown_keys(raw: dict, known, *, source: str) -> list:
    """檔案裡有、但本模組不認得的頂層鍵 → 出聲一次。回傳那些鍵（給測試看）。

    與 `_bot_config` / `_batch_config` 的同名函式**行為必須一致**（`_warn_once`
    已經是三份了，這是第三份的第二個函式）。三份的重複是刻意延後的架構決定，見
    重複可以，**分歧不行**——`test_presence_probe` 有一支拿同一組
    輸入問三邊。

    打錯鍵名的症狀跟值寫壞一模一樣：那個設定沒生效。而這個檔案的失效更難察覺，
    因為「音樂偵測沒反應」跟「現在真的沒在播」長得一樣。

    `_` 開頭是本專案在 JSON 裡寫註解的慣例（`presence_music.json` 現在有 5 個），
    不算未知。只印鍵名不印值——值是使用者填的視窗標題／AUMID 字串，而 stderr 會進
    log、`/log tail` 會把 log 送進對話平台。
    """
    unknown = sorted(key for key in raw
                     if key not in known and not str(key).startswith("_"))
    if unknown:
        shown = ", ".join(str(k)[:40] for k in unknown[:8])
        more = "" if len(unknown) <= 8 else f"（另有 {len(unknown) - 8} 個）"
        _warn_once(
            f"{source}: 不認得這些設定鍵，已忽略：{shown}{more}。"
            "鍵名打錯的話設定不會生效，而且沒有其他症狀——請對照預設值表確認拼字。")
    return unknown


# `presence_music.json` 的固定 schema。**`presence_games.json` 沒有這一份**：
# 那份是「行程名 → 標籤」的對照表，任意鍵就是它的資料。
_MUSIC_KNOWN_KEYS = frozenset({
    "smtc_source_substrings", "browser_aumid_substrings",
    "browser_window_hints", "foreground_window_patterns",
})


def _clean_str_list(raw, *, key: str, current: tuple, file_name: str) -> tuple:
    """把設定檔裡的字串清單正規化成小寫 tuple；不堪用就沿用 `current`。

    三條規則都是為了同一件事——**不要安靜地把偵測關掉**：

    * 不是清單 → 沿用預設並說一聲。
    * 清單裡有寫壞的項目（不是字串、或去空白後是空字串）→ **丟掉那幾筆、保留
      其餘可用的**，並說一聲。原本的寫法是 `all(isinstance(s, str))` 一票否決：
      五個來源裡有一個打成數字，五個就**全部**沒生效，而且一個字都沒印。
    * 清理完是空的 → **刻意不採用**，沿用預設並說一聲。空的白名單會讓比對永遠
      不命中，效果等同偷偷關掉偵測，而那跟「設定根本沒生效」長得一模一樣——
      這是本模組最典型的失效方式。

    這條規則不是這裡發明的：`_load_claude_detection` 的 `process_names` 已經這樣
    裁定過（見那裡的註解），這支只是把同一條套到音樂設定剩下的幾個鍵上。
    """
    if raw is _UNSET or raw is None:
        return current
    if not isinstance(raw, list):
        _warn_once(f"presence_probe: {file_name} 的 {key} 不是清單（收到 "
                   f"{type(raw).__name__}），已忽略、沿用預設 {list(current)}")
        return current
    cleaned = tuple(s.strip().lower() for s in raw
                    if isinstance(s, str) and s.strip())
    if not cleaned:
        _warn_once(f"presence_probe: {file_name} 的 {key} 沒有任何可用的字串，"
                   f"已忽略、沿用預設 {list(current)}")
        return current
    if len(cleaned) != len(raw):
        _warn_once(f"presence_probe: {file_name} 的 {key} 有 "
                   f"{len(raw) - len(cleaned)} 筆不是可用的字串，已跳過那幾筆、"
                   f"採用其餘 {list(cleaned)}")
    return cleaned


# --------------------------------------------------------------------------
# 視窗查詢後端
# --------------------------------------------------------------------------
# 前景視窗標題、前景視窗的行程、可見視窗清單——三件事本模組都要，實作全部在
# 桌面自動化函式庫（`je_auto_control`）裡，這裡只做一個有快取的取用點。本專案
# 曾經自己用 `win32gui` / `win32process` 寫過四份，那是最後一塊平行實作。
#
# **匯入成本是刻意延後的**：`je_auto_control` 一載就是 740 個模組、約 0.6 秒
# （會拉進影像相依），所以不要在 module 層 import。第一次 presence 探測會付這
# 個成本一次，之後都是快取。
_WINDOW_API = None
_WINDOW_API_TRIED = False


def _window_api():
    """回傳函式庫的視窗查詢模組；載不進來（非 Windows／沒安裝）回 None。"""
    global _WINDOW_API, _WINDOW_API_TRIED  # pylint: disable=global-statement
    if not _WINDOW_API_TRIED:
        _WINDOW_API_TRIED = True
        try:
            from je_auto_control.wrapper import auto_control_window  # type: ignore
            _WINDOW_API = auto_control_window
        except Exception as error:  # pylint: disable=broad-except
            print(f"presence_probe: window api unavailable: {error!r}",
                  file=sys.stderr)
            _WINDOW_API = None
    return _WINDOW_API


def _visible_windows() -> list[tuple[int, str]]:
    """所有可見且有標題的視窗，`(hwnd, 標題)`，最前面的在前。"""
    api = _window_api()
    if api is None:
        return []
    try:
        return list(api.list_windows(titled_only=True))
    except Exception:  # pylint: disable=broad-except
        return []


def _as_text(value) -> str:
    """把外部來源（JSON / SMTC / 視窗標題）的值安全轉成 stripped 字串。

    這是本模組唯一的字串入口。直接對外部值呼叫 `.strip()` / `[:128]` 在型別
    不如預期時會拋 AttributeError / TypeError，而本模組的例外會讓 bot 的
    presence 迴圈少掉一次 tick（或在有其他 bug 時整條迴圈死掉）。
    None / bool / list / dict 等非文字值一律回空字串，讓 caller 用
    `if not text:` 就能 fail-closed；只有數字會被字串化。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


# ---------- 遊戲：psutil process 名單 ---------------------------------------

def _read_games_json() -> dict:
    """Raw read of presence_games.json — 一份原始 dict。失敗回 {}。"""
    try:
        raw = GAMES_FILE.read_text(encoding=_CONFIG_ENCODING)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        _warn_once(f"presence_probe: read {GAMES_FILE.name} failed: {error!r}")
        return {}
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        _warn_once(f"presence_probe: parse {GAMES_FILE.name} failed: {error!r}")
        return {}
    return data if isinstance(data, dict) else {}


def _normalise_game_key(key: str) -> tuple[str, bool]:
    """把 JSON 的 key 正規化成 `(exe 檔名, 是不是 priority)`。

    **這是兩個載入器唯一的正規化來源。** `_load_game_whitelist` 與
    `_load_priority_games` 必須對同一個 key 算出同一個字串，否則 priority 會**靜默
    失效**——白名單認得那個遊戲、priority 清單認不得，於是它跑起來時不會贏過音樂
    偵測，而畫面上看起來就只是「presence 顯示得怪怪的」。

    **`*` 之前的空白也要處理。** 原本兩邊都寫 `k.lstrip("*").strip().lower()`，
    也就是**先剝 `*` 再 strip**。那個順序對 `"* endfield.exe"`（星號**後**有空白）
    是對的，對 `" *endfield.exe"`（星號**前**有空白）卻整個壞掉：`lstrip("*")` 遇到
    開頭的空白就停手，於是算出來的 key 是 `"*endfield.exe"`——一個**永遠不可能**
    對上任何行程名稱的字串，同時 `startswith("*")` 也是 False，所以 priority 也沒
    了。**兩個功能一起靜默死掉，而 JSON 看起來完全正常。**
    2026-09-07 實測確認：非 priority 的 `"  Endfield.EXE "` 前後空白都沒問題，
    唯獨 `*` 前面的空白會炸——這種只在一個位置成立的例外，人是看不出來的。
    正確順序是**先 strip、再判 `*`、再剝、再 strip**。

    這個檔案是**給人手動編輯**的，而且每 ~8 秒熱重載、不必重啟 bot，所以多打一個
    空白是完全可能發生的事，而它不會有任何錯誤訊息。
    """
    stripped = key.strip()
    is_priority = stripped.startswith("*")
    return stripped.lstrip("*").strip().lower(), is_priority


def _is_comment_key(key: str) -> bool:
    """`_` 開頭的 key 是註解，不是遊戲。

    `presence_games.json` 自己的 `_comment` 就寫著「Comment keys (prefix '_')
    are ignored by the matcher」——但**在 2026-09-07 之前那句話是假的**：兩個
    `_comment` 條目照樣被收進白名單，`loaded_game_whitelist()`（診斷指令會印它）
    因此多出兩筆「遊戲」，顯示名稱是整段說明文字。實際比對不到任何行程所以沒有
    造成故障，但**一份自己說謊的資料契約，下一個人會照著它寫程式**。

    **這是唯一一份，消費端不要再濾一次。** bot 的診斷指令
    （`discord_bot.cmd_probe_status`）直接印 `loaded_game_whitelist()`。它曾經自己
    再濾一次 `startswith("_")`，而那一句套在正規化之後的 key 上，所以這支函式就算
    壞掉，唯一看得到白名單內容的地方也不會有任何變化（2026-09-12 拿掉；
    `test_bot_presence_logging` 事情三那兩支釘著）。
    """
    return key.strip().startswith("_")


def _load_game_whitelist() -> dict[str, str]:
    """讀 presence_games.json；不存在或壞掉就回空 dict。
    Key 一律小寫、`*` 前綴會被剝掉（priority 標記）；caller 也用小寫比對。
    正規化走 `_normalise_game_key`（單一來源），註解 key 走 `_is_comment_key`。

    空 key 與空/非字串的顯示名稱一律跳過：
      * 空 key（JSON 寫成 `""` 或只有 `"*"`）會變成 whitelist 裡的 `""`，
        而 caller 對「psutil 取不到名稱的行程」算出的 name 也是 `""` ——
        於是每個存取被拒的行程都會誤判成命中，presence 卡在一個假遊戲上。
      * 空/None 的顯示名稱會讓狀態顯示成 `Playing None`。
    """
    data = _read_games_json()
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or _is_comment_key(k):
            continue
        clean, _is_priority = _normalise_game_key(k)
        display = _as_text(v)
        if not clean or not display:
            continue
        out[clean] = display
    return out


def _load_priority_games() -> list[str]:
    """回 JSON 內以 `*` 開頭的 exe key（去 `*`、lower、依 JSON 出現順序）。
    這些遊戲被 `probe_game_process` 視為「最高優先」— 任何時候
    在跑就贏，包括蓋過音樂偵測。沒有 priority entry 回空 list。

    正規化方式必須與 `_load_game_whitelist` 完全一致，否則像 `"* endfield.exe"`
    這種多打一個空白的 key 會在兩邊算出不同字串，priority 就靜默失效。
    **所以兩邊都走 `_normalise_game_key`——那是唯一的來源，不要在這裡另寫一份。**
    「兩份平行的正規化規則，總有一份會被改漏」正是這個函式原本踩到的坑（見
    `_normalise_game_key` 的 docstring）。"""
    data = _read_games_json()
    out: list[str] = []
    for k in data.keys():
        if not isinstance(k, str) or _is_comment_key(k):
            continue
        clean, is_priority = _normalise_game_key(k)
        if is_priority and clean:
            out.append(clean)
    return out


def probe_priority_game() -> str | None:
    """專門查 priority 遊戲（JSON `*` 前綴）有沒有在跑。回顯示名稱或
    None。給 `probe_game_process` 用來在「音樂偵測之前」攔截 —
    使用者邊聽 Spotify 邊玩 Endfield 時想看到 'Playing Endfield' 而不是
    'Listening to ...'。"""
    priority = _load_priority_games()
    if not priority:
        return None
    whitelist = _load_game_whitelist()
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    running: set[str] = set()
    try:
        for proc in psutil.process_iter(attrs=["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in whitelist:
                running.add(name)
    except Exception as error:  # pylint: disable=broad-except
        print(f"presence_probe: priority scan failed: {error!r}",
              file=sys.stderr)
        return None
    # 依 JSON 順序逐個比對，第一個 running 的 priority 遊戲就贏。
    for exe in priority:
        if exe in running:
            return whitelist.get(exe)
    return None


def _foreground_window_pid() -> int | None:
    """目前前景視窗對應的行程 pid；查不到回 None。

    用 pid 而不是視窗標題來認人：標題是程式想顯示什麼就顯示什麼，行程不是。
    """
    api = _window_api()
    if api is None:
        return None
    try:
        return api.foreground_window_process_id()
    except Exception:  # pylint: disable=broad-except
        return None


def probe_game_process() -> str | None:
    """掃所有 process、回對應 presence_games.json 的顯示名稱。

    挑選優先順序：
    1. **Priority 遊戲**（JSON key 以 `*` 開頭）— 任何時候在跑就贏，
       連前景視窗都不看（也會被 `probe_signals_async` 拿來覆蓋
       音樂偵測）。
    2. **前景視窗對應的 game process** — 同時開多個非 priority 遊戲時
       挑你正在玩的那個。
    3. **第一個 whitelist 命中**（psutil 順序）— fallback。
    """
    whitelist = _load_game_whitelist()
    if not whitelist:
        return None
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    priority = _load_priority_games()
    fg_pid = _foreground_window_pid()
    running: set[str] = set()
    fg_match: str | None = None
    first_hit: str | None = None
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            name = (proc.info.get("name") or "").lower()
            if name not in whitelist:
                continue
            running.add(name)
            display = whitelist[name]
            if fg_pid and proc.info.get("pid") == fg_pid and fg_match is None:
                fg_match = display
            if first_hit is None:
                first_hit = display
    except Exception as error:  # pylint: disable=broad-except
        print(f"presence_probe: process scan failed: {error!r}", file=sys.stderr)
        return None
    # 1. Priority winners — JSON 順序、第一個在跑的贏。
    for exe in priority:
        if exe in running:
            return whitelist.get(exe)
    # 2. Foreground-window game.
    if fg_match:
        return fg_match
    # 3. First whitelist match.
    return first_hit


# ---------- 音樂：SMTC via PowerShell ---------------------------------------

# PowerShell 片段：列出所有 SMTC session、取出正在 Playing 的那筆的 Title /
# Artist / SourceAppUserModelId。winsdk 在 Python 3.14 沒 prebuilt wheel 且
# 從原始碼編譯要 Visual Studio，所以改走 PowerShell 直接呼叫 Windows.Media
# .Control 的 WinRT 介面。
#
# 編碼處理：PowerShell 5.1 的 `[Console]::OutputEncoding = UTF8` 在子行程情
# 境下不一定生效（pipeline 的 Out-Default 會用系統 codepage 重新編碼），會
# 把中文 / 日文標題輸出成 cp950 的 mojibake。最穩的做法是在 PowerShell 端
# 把 JSON 字串轉成 UTF-8 bytes，直接寫到 `[Console]::OpenStandardOutput()`，
# 完全繞過 PowerShell 的輸出編碼層。
#
# 注意：必須留在 PowerShell 5.1。PowerShell 7 不能呼叫 `Windows.Media.
# Control` 的 WinRT API（會丟 "Operation is not supported on this platform"
# 0x80131539），所以即使有裝 `pwsh.exe` 也不能拿來跑這段。
_PS_PROBE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($action, $resultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
    $netTask = $asTask.Invoke($null, @($action))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
$mgr = Await ($mgrType::RequestAsync()) $mgrType
$propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
$result = $null
foreach ($s in $mgr.GetSessions()) {
    $info = $s.GetPlaybackInfo()
    if ($info.PlaybackStatus -eq 'Playing') {
        $props = Await ($s.TryGetMediaPropertiesAsync()) $propsType
        $result = [pscustomobject]@{
            title  = $props.Title
            artist = $props.Artist
            source = $s.SourceAppUserModelId
            playbackType = [string]$props.PlaybackType
        }
        break
    }
}
if ($result) { $json = $result | ConvertTo-Json -Compress } else { $json = '{}' }
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
$stream = [Console]::OpenStandardOutput()
$stream.Write($bytes, 0, $bytes.Length)
$stream.Flush()
"""

# Defaults for `presence_music.json` (defined inline so the file is optional
# and the historical hardcoded behavior is preserved if it's missing /
# malformed). See `_load_music_rules` for the loader.
#
# `smtc_source_substrings`: substrings (case-insensitive) checked against the
# SMTC AUMID. A hit = approved streaming music app/PWA → listen to its
# title/artist. Local-file players are deliberately excluded.
# `browser_aumid_substrings`: substrings identifying a generic browser SMTC
# (no per-PWA AUMID). Generic browser SMTC is NOT music by itself — would
# false-positive on regular YouTube / Meet / lecture videos — so it goes
# through the window-title verification step below.
# `browser_window_hints`: each entry is (window_substring, label). A browser
# SMTC is promoted to music only when SOME visible window contains both the
# window_substring AND the SMTC song title.
# `foreground_window_patterns`: regexes matched against the foreground
# window title when SMTC returns nothing. Must contain named group `song`;
# `artist` is optional.
_DEFAULT_SMTC_SUBSTRINGS = (
    "spotify", "ytmusic", "youtube music", "applemusic", "apple music",
    "tidal", "deezer", "qqmusic", "neteasemusic", "kkbox",
)
_DEFAULT_BROWSER_AUMID_SUBSTRINGS = (
    "msedge", "chrome", "firefox", "brave", "opera", "vivaldi",
)
_DEFAULT_BROWSER_WINDOW_HINTS = (
    ("youtube music", "youtube music"),
)
_DEFAULT_FG_PATTERN_STRINGS = (
    r"^(?P<song>.+?) - (?P<artist>.+?) - YouTube Music",
    r"^Spotify - (?P<song>.+?) - (?P<artist>.+)$",
    r"^(?P<song>.+?) • (?P<artist>.+?) - Spotify",
    r"^(?P<song>.+?) by (?P<artist>.+?) - Apple Music",
)


def _compile_patterns(pattern_strings) -> tuple:
    """編譯前景視窗標題的 pattern；每一種丟棄都要說一聲。

    **`song` 這個具名 group 是必要條件，而在這之前沒有任何東西在檢查它。**
    `probe_foreground_music` 的 docstring 從一開始就寫著「Pattern MUST contain
    named group `song`」，但少了它的 pattern 照樣編譯得過、照樣被收進清單，然後
    在比對時 `groups.get("song")` 回 `None` → `song` 變成空字串 → 那一筆被跳過。
    結果是一個**看起來設定好了、實際上永遠不會命中**的 pattern，而且完全無聲。
    一條寫在 docstring 裡、沒有任何東西執行的規則，等於沒有這條規則。
    """
    out = []
    for s in pattern_strings:
        if not isinstance(s, str):
            # 原本是靜默 `continue`：JSON 裡把 pattern 寫成數字或物件時，那一筆
            # 就這樣消失了。壞掉的 regex 會出聲，寫錯型別的卻不會，沒有道理。
            _warn_once(f"presence_probe: {MUSIC_FILE.name} 的 "
                       f"foreground_window_patterns 有一筆不是字串（收到 "
                       f"{type(s).__name__}），已跳過")
            continue
        try:
            compiled = re.compile(s)
        except re.error as error:
            _warn_once(f"presence_probe: bad regex {s!r}: {error}")
            continue
        if "song" not in compiled.groupindex:
            _warn_once(f"presence_probe: {MUSIC_FILE.name} 的 pattern {s!r} "
                       f"沒有 (?P<song>...) 這個具名 group，永遠不會命中，"
                       f"已跳過")
            continue
        out.append(compiled)
    return tuple(out)


def _load_music_rules() -> dict:
    """Read `presence_music.json` on every call; fall back to hardcoded
    defaults on missing / parse / type errors. Symmetric with
    `_load_game_whitelist` — edits to the JSON take effect on the next
    probe tick (~8s). No caching: 3 disk hits per probe tick is trivial
    next to the PowerShell SMTC subprocess."""
    defaults = {
        "smtc_source_substrings": _DEFAULT_SMTC_SUBSTRINGS,
        "browser_aumid_substrings": _DEFAULT_BROWSER_AUMID_SUBSTRINGS,
        "browser_window_hints": _DEFAULT_BROWSER_WINDOW_HINTS,
        "foreground_window_patterns": _compile_patterns(
            _DEFAULT_FG_PATTERN_STRINGS),
    }
    try:
        text = MUSIC_FILE.read_text(encoding=_CONFIG_ENCODING)
    except FileNotFoundError:
        return defaults
    except (OSError, UnicodeDecodeError) as error:
        _warn_once(f"presence_probe: read {MUSIC_FILE.name} failed: {error!r}")
        return defaults
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as error:
        _warn_once(f"presence_probe: parse {MUSIC_FILE.name} failed: {error!r}")
        return defaults
    if not isinstance(data, dict):
        return defaults
    _warn_unknown_keys(data, _MUSIC_KNOWN_KEYS, source="presence_probe")
    # 底下四個鍵共用同一條規則：**清理完是空的就不採用**。四個都是白名單／
    # pattern 清單，空的那個 tuple 會讓比對永遠不命中，於是音樂偵測安靜地關掉，
    # 而使用者看到的症狀跟「現在真的沒在播」一模一樣。裁定與理由見
    # `_clean_str_list` 與 `_load_claude_detection`。
    name = MUSIC_FILE.name
    defaults["smtc_source_substrings"] = _clean_str_list(
        data.get("smtc_source_substrings", _UNSET), key="smtc_source_substrings",
        current=defaults["smtc_source_substrings"], file_name=name)
    defaults["browser_aumid_substrings"] = _clean_str_list(
        data.get("browser_aumid_substrings", _UNSET),
        key="browser_aumid_substrings",
        current=defaults["browser_aumid_substrings"], file_name=name)
    hints = data.get("browser_window_hints", _UNSET)
    if isinstance(hints, list):
        parsed_hints = []
        for entry in hints:
            if (isinstance(entry, dict)
                    and isinstance(entry.get("window_substring"), str)
                    and isinstance(entry.get("label"), str)):
                parsed_hints.append(
                    (entry["window_substring"].lower(), entry["label"]))
        if parsed_hints:
            if len(parsed_hints) != len(hints):
                _warn_once(f"presence_probe: {name} 的 browser_window_hints 有 "
                           f"{len(hints) - len(parsed_hints)} 筆缺 "
                           f"window_substring／label 或型別不對，已跳過那幾筆")
            defaults["browser_window_hints"] = tuple(parsed_hints)
        else:
            _warn_once(f"presence_probe: {name} 的 browser_window_hints 沒有任何"
                       f"可用的項目（每一筆都要有字串的 window_substring 與 "
                       f"label），已忽略、沿用預設")
    elif hints is not _UNSET and hints is not None:
        _warn_once(f"presence_probe: {name} 的 browser_window_hints 不是清單"
                   f"（收到 {type(hints).__name__}），已忽略、沿用預設")
    patterns = data.get("foreground_window_patterns", _UNSET)
    if isinstance(patterns, list):
        compiled = _compile_patterns(patterns)
        if compiled:
            defaults["foreground_window_patterns"] = compiled
        else:
            # `_compile_patterns` 已經逐筆說過為什麼被丟掉，這裡只補「所以結果
            # 是沿用預設」——少了這句，使用者會以為自己的 pattern 生效了。
            _warn_once(f"presence_probe: {name} 的 foreground_window_patterns "
                       f"沒有任何可用的 pattern，已忽略、沿用預設")
    elif patterns is not _UNSET and patterns is not None:
        _warn_once(f"presence_probe: {name} 的 foreground_window_patterns "
                   f"不是清單（收到 {type(patterns).__name__}），"
                   f"已忽略、沿用預設")
    return defaults


# 收掉「已經被 kill 的」SMTC 探測子行程用的上限。**刻意比 `dorossi_backend` 的
# `_DOROSSI_REAP_TIMEOUT_SEC`（10 秒）短一個量級**，因為兩邊的處境不同：後端那支
# 一輪跑幾分鐘，收尾多花十秒無所謂；這支每 `presence_probe_interval_sec`（預設 8）
# 秒跑一次，而 presence 迴圈是「做完事再 sleep」——收尾花掉的時間會**直接加進
# 週期**。已經逾時的那一輪本身就燒掉了 `timeout` 秒（預設 6），再花十秒收屍等於
# 整整漏掉一個以上的 tick。
#
# 1.5 秒的來源是實測而不是猜的：`kill()` 之後 `returncode` 約 0.17～0.25 秒就出現
# （作業系統層的離開被觀察到就設好，**與管線無關**），所以 1.5 秒有大約 6 倍餘裕。
# 收不掉時正確的反應是**放棄收屍、記一句、回 None 讓下一輪照常發生**，不是把整個
# presence 迴圈押在一個不會結束的等待上。
_SMTC_REAP_TIMEOUT_SEC = 1.5
# 回頭看一眼 `returncode` 的間隔。這不是輪詢式的等待——`wait()` 一完成就會立刻返回
# （用的是 `asyncio.wait` 不是 `sleep`），這個值只決定「管線被孫行程握著」那條路上
# 多久發現得了 rc。
_SMTC_REAP_POLL_SEC = 0.05


async def _reap_timed_out_probe(proc, timeout: float | None = None) -> None:
    """把一個**已經被 kill 的**探測子行程收掉，最多花 `timeout` 秒。

    **為什麼不能只寫 `await proc.wait()`**（2026-09-09 本機實測，CPython 3.14 /
    Windows）：`BaseSubprocessTransport._wait()` 把自己掛在 `_exit_waiters` 上，那批
    waiter **只有** `_call_connection_lost` 會叫醒，而 `_try_finish` 要求
    `all(p.disconnected)`——也就是 **stdout 與 stderr 都要先 EOF**。這支起的是
    `powershell … Add-Type` ＋ WinRT，會生孫行程；只要有一個孫行程繼承著這兩條管線
    的寫端，`await proc.wait()` 就**永遠不返回**（不是慢，是無限）。而 Windows 的
    `proc.kill()` 是 `TerminateProcess`，只帶走直接子行程、帶不走孫行程，所以「先
    kill 再 await」擋不住。舊寫法外面那個 `except Exception` 也擋不住——**掛住不是
    例外**。量到的數字：kill 之後 0.17 秒 `returncode` 已經是 7，而同一個 pending 的
    `wait()` 三秒後仍未完成，殺掉孫行程的瞬間才返回。

    卡住的後果不是「這一輪沒抓到音樂」，是**整個 presence 迴圈永久停住**：
    `_presence_probe_loop` 直接 await `probe_signals_async()`，而它外面那個
    `except Exception` 同樣接不到掛住。症狀是 presence 凍在最後狀態——正好是本模組
    一再強調「跟『現在真的沒在播』分不出來」的那種安靜失敗。

    所以這裡**同時**盯兩個訊號，誰先到算誰：`wait()` 完成（管線正常關閉的路徑，零
    額外延遲）與 `returncode` 出現（管線被握著的路徑）。**不要**寫成「先 `wait_for`
    滿一個 timeout、逾時再看 returncode」——那會在管線被握著時白等滿上限。

    逾時就放棄收屍，但**一定要**把 `wait()` 那個 task cancel + gather 掉：只加上限
    不收任務，只是把「永遠卡住」換成「每 8 秒多一個孤兒任務」。

    `timeout=None` → 在**呼叫時**查模組常數（不要寫成預設引數：預設引數在 `def` 當下
    就固定住，之後改模組常數不會生效，測試也就換不掉那個值）。
    """
    if timeout is None:
        timeout = _SMTC_REAP_TIMEOUT_SEC
    if proc.returncode is not None:
        return
    wait_task = asyncio.ensure_future(proc.wait())
    try:
        deadline = time.monotonic() + timeout
        while proc.returncode is None and not wait_task.done():
            if time.monotonic() >= deadline:
                print("presence_probe: SMTC probe did not reap within "
                      f"{timeout}s (a grandchild is still holding the pipes); "
                      "giving up so the next tick stays on schedule",
                      file=sys.stderr)
                return
            # `asyncio.wait` 而不是 `sleep`：`wait()` 一完成就馬上回來（正常路徑零
            # 額外延遲），同時每 poll 秒有機會回頭看一眼 `returncode`。
            await asyncio.wait({wait_task}, timeout=_SMTC_REAP_POLL_SEC)
    finally:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)


async def probe_smtc_raw_async(timeout: float = 6.0) -> dict | None:
    """跑 PowerShell 拿正在 Playing 的第一個 SMTC session，回傳 raw 資料
    （**不**做 source 白名單過濾），給 /probe_status 診斷用。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_PROBE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as error:
        print(f"presence_probe: powershell launch failed: {error!r}",
              file=sys.stderr)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=timeout)
    except asyncio.TimeoutError:
        # 逾時也要說一聲：這條路整個安靜掉的話，音樂狀態會永遠是「沒在播」，
        # 而那跟「真的沒在播」長得一模一樣。
        print(f"presence_probe: SMTC probe timed out after {timeout}s",
              file=sys.stderr)
        # `kill()` 要包起來，理由**不是**泛泛的防禦性：`asyncio` 的
        # `Process.kill()` 走 `BaseSubprocessTransport.kill()` → `_check_proc()`，
        # 而 `_check_proc()` 在 `self._proc is None` 時直接丟 `ProcessLookupError`；
        # `_proc` 是 `_call_connection_lost` 設成 None 的。也就是說「子行程已經結束、
        # connection_lost 已經跑過」之後再呼叫 `kill()` 一定會丟，**而且與訊號無關、
        # 每個平台都一樣**。CLAUDE.md 那句「Windows 上 `except ProcessLookupError`
        # 是死碼」的範圍只涵蓋 `os.kill(pid, 0)` 那種拿訊號當存活探測的寫法，對
        # 這裡不成立（2026-09-20 本機實測：Windows 11 / CPython 3.14.4，子行程結束
        # 後連呼叫兩次 `kill()`，兩次都拿到 `ProcessLookupError`）。
        # 競態是真的走得到的：`asyncio.wait_for` 逾時會先取消內層任務，而取消的那
        # 幾個 await 之間 event loop 有機會把 `_process_exited` /
        # `_call_connection_lost` 跑完——PowerShell 子行程剛好在逾時那一瞬間結束時
        # 就會踩到。代價不大（呼叫端的 `except Exception` 會接住，這一次 tick 的
        # presence 訊號整組落空），但這支每幾秒跑一次，次數很多。
        # `except Exception` 那一層也留著：`kill()` 在極端情況下還可能丟別的
        # `OSError`，而這裡已經在逾時收尾路徑上，任何例外都不該取代「這次探測沒有
        # 結果」這個結論。
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        # kill() 只是送出訊號，不收的話子行程會留成殭屍，而這支每幾秒就跑一次
        # ——累積起來是真的。但收屍**必須有上限**：原本這裡是裸的
        # `await proc.wait()` ＋ 一個 `except Exception`，而 `wait()` 等的是**管線
        # EOF** 不是行程結束，掛住又不是例外，所以那個 except 一點用都沒有；
        # PowerShell 起的孫行程握著寫端時，整個 presence 迴圈會從此永久停住。
        await _reap_timed_out_probe(proc)
        return None
    except BaseException:
        # 取消（bot 關機／`/sys restart` 會把 presence 迴圈的 task cancel 掉，
        # `/probe_status` 那條互動也可能被取消）落在這裡，而 `CancelledError` 是
        # `BaseException`，上面那個 `except asyncio.TimeoutError` 接不到它。
        # **同步**先砍：取消路徑上不保證還有機會跑完任何 await（與
        # `dorossi_backend._dorossi_via_claude_code` 同一條理由），而
        # `proc.kill()` 不是 coroutine，一定跑得完。這裡刻意不 await 收屍——
        # 行程已經拿到 TerminateProcess，而再多一個 await 正是取消路徑上不保證
        # 跑得完的東西。
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        raise
    if proc.returncode != 0:
        # 這裡本來是 `stdout, _ = …` 然後直接 return None：stderr **被接了管線
        # 然後丟掉**，比不接還糟——不接的話錯誤訊息至少會落到主控台。
        # 這條路依賴的東西不少（PowerShell 5.1、WinRT 的 Windows.Media.Control、
        # Add-Type、執行原則），任何一個變了都只會表現成「音樂狀態永遠是空的」，
        # 而那跟「現在沒在播音樂」無法區分。截斷是因為 PowerShell 的錯誤訊息很長，
        # 而每幾秒就會再印一次。
        detail = stderr.decode("utf-8", errors="replace").strip()
        print(f"presence_probe: SMTC probe exited rc={proc.returncode}"
              f"{': ' + detail[:400] if detail else ''}", file=sys.stderr)
        return None
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text or text == "{}":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # 一律走 `_as_text`。這四行原本是 `(data.get(k) or "").strip()`，而
    # `_as_text` 的 docstring 寫著它是「本模組唯一的字串入口」——這裡就是那條規則
    # 唯一沒被遵守的地方。差別在型別不對的時候：`123 or ""` 的結果是 `123`，
    # 接著 `.strip()` 丟 `AttributeError`，而這支是 `probe_signals_async` 的一環，
    # 一次例外就少掉一個 presence tick。資料來自另一個行程（PowerShell）的
    # stdout，本專案對跨行程資料的立場一向是「不要相信型別」。
    title = _as_text(data.get("title"))
    artist = _as_text(data.get("artist"))
    source = _as_text(data.get("source"))
    playback_type = _as_text(data.get("playbackType"))
    if not title:
        return None
    return {"title": title, "artist": artist, "source": source,
            "playback_type": playback_type}


def _source_is_music(source: str) -> bool:
    source_lower = source.lower()
    return any(p in source_lower
               for p in _load_music_rules()["smtc_source_substrings"])


def _source_looks_like_browser(source: str) -> bool:
    source_lower = source.lower()
    return any(h in source_lower
               for h in _load_music_rules()["browser_aumid_substrings"])


def _smtc_song_in_browser_music_window(song_title: str) -> str | None:
    """Disambiguate browser-tab SMTC sessions.

    `MSEdge` / `Chrome` AUMID can be YouTube Music in a normal tab OR a
    regular YouTube video / Meet / 教學影片. We're only confident it's
    YT Music if some visible window contains both `song_title` AND the
    YT Music marker (`"youtube music"`). Returns the music label on hit,
    None otherwise. Length-2 minimum on song_title to avoid noise.
    """
    if not song_title or len(song_title.strip()) < 2:
        return None
    needle = song_title.strip().lower()
    hints = _load_music_rules()["browser_window_hints"]
    if not hints:
        return None
    for _hwnd, raw_title in _visible_windows():
        title = (raw_title or "").lower()
        if needle not in title:
            continue
        for kw, label in hints:
            if kw in title:
                return label
    return None


def _filter_smtc_media(raw: dict | None) -> dict | None:
    """把 raw SMTC 結果套上串流音樂白名單：source 命中核准的串流 app → 收；
    一般瀏覽器分頁（MSEdge / Chrome …）走視窗標題備援確認是不是 YT Music；
    本機檔案播放器與其他來源回 None。抽成同步 helper 讓 caller 能對「已經抓好
    的 raw」重複套用，不必再跑一次 PowerShell。"""
    if raw is None:
        return None
    if _source_is_music(raw["source"]):
        return raw
    if _source_looks_like_browser(raw["source"]):
        label = _smtc_song_in_browser_music_window(raw["title"])
        if label:
            return {**raw, "source": f"{raw['source']} ({label} tab)"}
    return None


async def probe_smtc_media_async(timeout: float = 6.0) -> dict | None:
    """白名單過濾後的 SMTC 結果。一般瀏覽器分頁的 SMTC（AUMID 是
    `MSEdge` / `Chrome` …）走視窗標題備援確認是不是 YT Music；其他
    瀏覽器內容（普通 YouTube、Meet）回 None。要看 raw 資料用
    `probe_smtc_raw_async`。"""
    return _filter_smtc_media(await probe_smtc_raw_async(timeout))


def probe_foreground_window_raw() -> str | None:
    """目前前景視窗的標題（不做任何 pattern match），給診斷用。"""
    api = _window_api()
    if api is None:
        return None
    try:
        hit = api.foreground_window()
    except Exception:  # pylint: disable=broad-except
        return None
    if hit is None:
        return None
    return (hit[1] or "").strip() or None


def list_running_processes() -> list[str]:
    """回傳所有 process exe 檔名（lowercase）。給 /probe_status 用，方便
    使用者比對白名單。"""
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    seen = set()
    try:
        for proc in psutil.process_iter(attrs=["name"]):
            n = (proc.info.get("name") or "").lower()
            if n:
                seen.add(n)
    except Exception:  # pylint: disable=broad-except
        return sorted(seen)
    return sorted(seen)


def loaded_game_whitelist() -> dict[str, str]:
    """暴露目前讀進來的白名單給診斷指令印出來。"""
    return _load_game_whitelist()


# ---------- 前景視窗：嚴格 pattern 抓音樂 -----------------------------------

def probe_foreground_music() -> dict | None:
    """SMTC 抓不到時的備援：解析前景視窗標題；只接受明確的音樂格式。
    Patterns come from `presence_music.json` (foreground_window_patterns)
    so users can add e.g. Apple Music desktop / TIDAL formats without code
    edits. Pattern MUST contain named group `song`; `artist` is optional
    (resolves to "" if absent)."""
    # 標題來源與 `probe_foreground_window_raw` 共用一支，不要各抓各的。
    title = probe_foreground_window_raw()
    if not title:
        return None
    for pat in _load_music_rules()["foreground_window_patterns"]:
        m = pat.match(title)
        if not m:
            continue
        groups = m.groupdict()
        song = (groups.get("song") or "").strip()
        artist = (groups.get("artist") or "").strip()
        if song:
            return {"title": song, "artist": artist, "source": "foreground"}
    return None


# ---------- Claude Code 偵測 ------------------------------------------------

def _load_claude_detection() -> tuple[bool, tuple[str, ...]]:
    """讀 presence_rpc.json 的 `claude` 區塊：`enabled`（要不要偵測）+
    `process_names`（要比對哪些 exe，lowercase）。檔案不存在 / 壞掉 / 型別
    不符 → 預設「啟用、偵測 claude.exe」。Claude 偵測預設開，因為使用者
    明確要把『正在用 Claude』當狀態；不想要就在 JSON 設
    `{"claude": {"enabled": false}}`。"""
    enabled = True
    names = _DEFAULT_CLAUDE_PROCESSES
    try:
        raw = RPC_CONFIG_FILE.read_text(encoding=_CONFIG_ENCODING)
    except FileNotFoundError:
        return enabled, names
    except (OSError, UnicodeDecodeError) as error:
        _warn_once(f"presence_probe: read {RPC_CONFIG_FILE.name} failed: {error!r}")
        return enabled, names
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        _warn_once(f"presence_probe: parse {RPC_CONFIG_FILE.name} failed: {error!r}")
        return enabled, names
    block = data.get("claude") if isinstance(data, dict) else None
    if isinstance(block, dict):
        # 「型別不符 → 退回預設」沿用全專案的慣例（`_bot_config._coerce_bool` 的
        # docstring 講明了：只收真正的 JSON boolean，`"true"` / `1` 一律不算）。
        # **但這裡多做一件事：退回時要吭聲。** `_bot_config` 的載入器本來就會對
        # 每個退回的鍵印一行警告，這個區塊卻是靜默的，而這個鍵的靜默特別糟——
        # 它是一個**隱私退出開關**。使用者寫了 `"enabled": "false"`（字串）本意是
        # 關掉，型別不符讓它退回預設值 True，於是「正在用 Claude Code」繼續被廣播
        # 出去，而使用者以為自己已經關掉了。兩種錯法的代價不對稱，所以至少要讓它
        # 留下痕跡。
        #
        # 用 sentinel 而不是 `.get(key)` 是因為 `null` 也是一種「寫了但寫錯」——
        # `.get()` 回 None 時分不出「沒寫」與「寫成 null」，而前者不該警告。
        raw_enabled = block.get("enabled", _UNSET)
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        elif raw_enabled is not _UNSET:
            _warn_once(f"presence_probe: {RPC_CONFIG_FILE.name} 的 "
                       f"claude.enabled 不是 JSON boolean（收到 "
                       f"{type(raw_enabled).__name__}），已忽略、沿用預設 "
                       f"{enabled}；要關掉請寫 false（不加引號）")
        pn = block.get("process_names", _UNSET)
        if isinstance(pn, list):
            cleaned = tuple(
                s.strip().lower() for s in pn
                if isinstance(s, str) and s.strip()
            )
            if cleaned:
                names = cleaned
            else:
                # 空清單／整份都是雜訊時**刻意不採用**，沿用預設。採用空的 tuple
                # 會讓比對永遠不命中，效果等同偷偷關掉偵測——那跟「設定沒生效」
                # 長得一模一樣，是這個模組最典型的失效方式。
                _warn_once(f"presence_probe: {RPC_CONFIG_FILE.name} 的 "
                           f"claude.process_names 沒有任何可用的字串，已忽略、"
                           f"沿用預設 {list(names)}")
        elif pn is not _UNSET:
            _warn_once(f"presence_probe: {RPC_CONFIG_FILE.name} 的 "
                       f"claude.process_names 不是清單（收到 "
                       f"{type(pn).__name__}），已忽略、沿用預設 {list(names)}")
    return enabled, names


def probe_claude_code() -> str | None:
    """偵測 Claude Code CLI 是否在跑（預設行程名 `claude.exe`）。回固定顯示
    名稱 'Claude Code' 或 None。給 `bot_activity_from_signals` /
    `rpc_activity_from_signals` 當**最低優先** fallback（沒遊戲、沒音樂時
    才顯示），也給 discord_rpc 拿來顯示寫程式狀態。"""
    enabled, names = _load_claude_detection()
    if not enabled:
        return None
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        for proc in psutil.process_iter(attrs=["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in names:
                return "Claude Code"
    except Exception as error:  # pylint: disable=broad-except
        print(f"presence_probe: claude scan failed: {error!r}", file=sys.stderr)
        return None
    return None


# ---------- 訊號 + 兩套優先序（bot 鏡像 vs 使用者自己帳號的 RPC）-----------
#
# bot 跟 RPC 想要不同的優先序，所以不能共用 probe_local_activity_async：
#   * bot 鏡像：遊戲 > 核准的串流音樂 > Claude（墊底）
#   * RPC（你自己帳號）：Claude > 遊戲 > 音樂（嚴格）
# 為了不重複跑 SMTC PowerShell（最貴的一步），這裡一次把所有訊號抓齊
# （`probe_signals_async`），再用兩個**純函式**各自組裝結果。

async def probe_signals_async() -> dict:
    """一次抓齊所有 presence 訊號（SMTC PowerShell 只跑一次），給 bot / RPC
    兩套優先序各自組裝。回:
      game         : str | None     — 命中的遊戲顯示名
      music_strict : dict | None    — 嚴格判定的音樂（SMTC 白名單 / 前景 pattern）
      music_any    : dict | None    — bot 使用的核准串流音樂（保留相容欄位名）
      claude       : str | None     — 'Claude Code' or None
    music_* 的 dict 形如 {"title","artist","source"}。"""
    game = probe_game_process()
    raw = await probe_smtc_raw_async()
    music_strict = _filter_smtc_media(raw)
    if not music_strict:
        music_strict = probe_foreground_music()
    # bot 也只接受核准的串流音樂。不要再退回 raw SMTC：raw 可能是 VLC、
    # 系統播放器、本機檔案或遊戲建立的 media session，會把非串流內容誤報為
    # Listening。Windows SMTC 沒提供可靠的本機檔案/串流旗標或 URI，所以只能
    # 採來源白名單；music_any 保留原欄位名以避免改動兩個 consumer 的介面。
    music_any = music_strict
    claude = probe_claude_code()
    return {
        "game": game,
        "music_strict": music_strict,
        "music_any": music_any,
        "claude": claude,
    }


def _media_to_activity(media: dict) -> dict | None:
    """SMTC 媒體 → listening activity。訊號畸形時回 None（讓呼叫端往下一個
    優先序掉），不要回一個名稱為空的 activity。

    `media.get("title", "")` 的預設值只在 key **不存在**時生效 —— SMTC 實際
    會回 `{"title": None}`，於是 `None[:128]` 直接 TypeError，而這個例外會從
    bot 的 presence 迴圈逸出、讓 presence 靜默凍結在最後狀態。"""
    if not isinstance(media, dict):
        return None
    title = _as_text(media.get("title"))
    artist = _as_text(media.get("artist"))
    display = f"{title} – {artist}" if (title and artist) else (title or artist)
    if not display:
        return None
    return {"kind": "listening", "name": display[:128]}


def bot_activity_from_signals(signals: dict) -> dict | None:
    """Bot 鏡像優先序：遊戲 > 核准的串流音樂 > Claude。
    Claude 墊底——只有完全沒遊戲、沒核准音樂在播時才顯示。"""
    game = _as_text(signals.get("game"))
    if game:
        return {"kind": "playing", "name": game[:128]}
    if signals.get("music_any"):
        activity = _media_to_activity(signals["music_any"])
        if activity is not None:
            return activity
    claude = _as_text(signals.get("claude"))
    if claude:
        return {"kind": "claude", "name": claude[:128]}
    return None


def rpc_activity_from_signals(signals: dict) -> dict | None:
    """RPC（使用者自己帳號）優先序：Claude > 遊戲 > 音樂（嚴格）。
    Claude 最高——只要 Claude Code 在跑，自己帳號就一律優先秀『在用 Claude』，
    連玩遊戲、聽音樂都蓋過。音樂這裡用嚴格判定（不放寬瀏覽器內容）。"""
    claude = _as_text(signals.get("claude"))
    if claude:
        return {"kind": "claude", "name": claude[:128]}
    game = _as_text(signals.get("game"))
    if game:
        return {"kind": "playing", "name": game[:128]}
    if signals.get("music_strict"):
        activity = _media_to_activity(signals["music_strict"])
        if activity is not None:
            return activity
    return None
