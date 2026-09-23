"""_code_fingerprint.py — 「這個行程正在跑的到底是哪一版程式碼？」

本專案的行程一次跑好幾天（webrunner 連續跑過 78.7 小時，bot 跑過 47.9 小時），
而 repo **在它們跑的同時被持續編輯**。這兩件事湊在一起會產生三個實際踩過的坑，
三個都只需要「啟動當下的程式碼指紋」就能當場回答：

1. **Traceback 的原始碼文字會騙人。** Python 的行號來自載入時的 code object，
   但顯示的那一行**文字是列印當下才從磁碟讀的**（`linecache`）。檔案被改過之後，
   兩者就對不起來了。2026-09-07 17:10:44 那份 traceback 是實例：frame 寫
   `_note_transport_error`（`_webrunner_shared.py` 第 1139 行），印出來的文字卻是
   `class BrowserGoneError(RuntimeError):`；而現在磁碟上第 1139 行是
   `except (OSError, UnicodeDecodeError):`，`_note_transport_error` 已經移到 1208。
   **三個版本互不相同。** 最危險的不是這種一看就荒謬的情形，是改動很小的時候
   ——那時錯誤的文字會看起來完全合理。
   判讀規則：**行號可信，文字只在指紋沒漂移時可信。**

2. **「修正到底有沒有生效？」** 這兩個行程都只在**啟動時**讀程式碼，沒有熱重載；
   webrunner 換角色重啟的是瀏覽器、不是行程。2026-09-08 為了回答這個問題，得靠
   比對行程啟動時間、檔案 mtime、還有 log 訊息的格式變化去反推——而且反推錯了一項
   （以為 `chromedriver.prev.log` 輪替沒上線，實際上它 00:56 才剛跑過）。

3. **`/version` 回答不了這個問題。** 它報的是 repo 的 `git HEAD`，而工作區可以
   （現在正是）帶著好幾天未提交的修改。HEAD 沒變不代表跑的程式碼沒變。

**設計上唯一的關鍵**：指紋必須在**啟動當下**算好並留住。等到要查的時候才算，
量到的是磁碟現況，而那正是我們要拿來比對的另一邊——會永遠回報「沒有漂移」。
所以 `snapshot()` 要由各個進入點在 import 完成之後**明確呼叫一次**。

**這個模組沒有解決、而且刻意不去解決的一件事**：它比對的是「啟動當下**磁碟上**的
內容」，不是「直譯器實際載入的那份 code object」。兩者之間有一個窗口——import 完成
到 `snapshot()` 被呼叫之間，如果剛好有人存檔，快照會記下**新**的內容，而行程跑的
是**舊**的，於是漂移永遠讀成 False。那正是本模組最糟的失效方式。

不去修的理由：真正無誤的來源（載入時的原始位元組）Python 並沒有留下來，而這個窗口
在實務上是毫秒等級——各進入點都把 `snapshot()` 放在 `main()` 的第一件事。用行程
啟動時間去比對檔案 mtime 可以把它變成「不知道」，但那要引入 psutil（本模組刻意只用
標準庫），代價高過收益。**寫在這裡是為了不要讓這個模組被過度信任**：它回答的是
「磁碟從我啟動之後有沒有再變」，而不是「我腦袋裡的位元組是不是磁碟上那一份」。

本模組是**被動的共用輔助模組**（與 `_batch_config` / `_chrome_slot` /
`_run_progress` 同屬 CLAUDE.md「允許的第三方通道」），只用標準庫，不寫任何檔案、
不起行程、不連外，import 本身沒有副作用。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

__all__ = [
    "FINGERPRINT_LEN",
    "snapshot",
    "snapshot_taken",
    "current_fingerprint",
    "drift_report",
    "describe",
]

FINGERPRINT_LEN = 12

PACKAGE_ROOT = Path(__file__).resolve().parent

# 啟動當下的快照：{相對路徑字串: sha256 十六進位}。None ＝ 還沒呼叫 `snapshot()`。
_SNAPSHOT: dict[str, str] | None = None
# 快照當下有幾個檔案讀不到。掃描不完整要說出來，不能靜靜當成「沒有漂移」
# （本專案的既有規則：失敗的列舉不可以偽裝成空的列舉）。
_SNAPSHOT_UNREADABLE: int = 0


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:FINGERPRINT_LEN]


def _project_source_files() -> list[Path]:
    """這個行程**實際載入**的本專案 `.py` 檔。

    走 `sys.modules` 而不是掃目錄，因為要問的是「載入了什麼」，不是「磁碟上有
    什麼」——一個沒被 import 的檔案改了，對這個行程沒有任何影響，把它算進去只會
    製造假的漂移警報。而會亂叫的警報最後會被人關掉（`test_language` 記過同一個
    教訓）。
    """
    seen: set[Path] = set()
    # `list(...)`：走訪期間別的執行緒仍可能 import 新模組，直接迭代會丟
    # RuntimeError: dictionary changed size during iteration。
    for module in list(sys.modules.values()):
        path_str = getattr(module, "__file__", None)
        if not path_str or not path_str.endswith(".py"):
            continue
        try:
            path = Path(path_str).resolve()
        except (OSError, ValueError):
            continue
        if path.parent == PACKAGE_ROOT or PACKAGE_ROOT in path.parents:
            seen.add(path)
    return sorted(seen)


def _read_all(paths) -> tuple[dict[str, str], int]:
    """`({相對路徑: 摘要}, 讀不到的檔案數)`。

    讀 **bytes** 而不是文字：指紋要的是位元組本身，解碼只會引入一個編碼問題
    （本機 `locale.getpreferredencoding()` 是 cp950），而且 UTF-8 以外的位元組
    會讓整個指紋直接爆掉。
    """
    out: dict[str, str] = {}
    unreadable = 0
    for path in paths:
        try:
            out[path.relative_to(PACKAGE_ROOT).as_posix()] = _digest(
                path.read_bytes())
        except (OSError, ValueError):
            unreadable += 1
    return out, unreadable


def snapshot() -> str:
    """記下**此刻**載入的程式碼指紋，回傳合併後的短指紋。

    由各進入點在自己的 import 都跑完之後呼叫一次。重複呼叫會覆寫——這是刻意的：
    重新 import 之後重新取樣是合理的，而擋下第二次只會讓呼叫端更難用。
    """
    global _SNAPSHOT, _SNAPSHOT_UNREADABLE
    _SNAPSHOT, _SNAPSHOT_UNREADABLE = _read_all(_project_source_files())
    return _combine(_SNAPSHOT)


def snapshot_taken() -> bool:
    """有沒有取過快照。沒取過時 `drift_report()` 只能回「不知道」。"""
    return _SNAPSHOT is not None


def _combine(files: dict[str, str]) -> str:
    """把每個檔案的摘要合成一個。路徑也餵進去，否則兩個檔案內容互換會同指紋。"""
    blob = "\n".join(f"{name}:{digest}" for name, digest in sorted(files.items()))
    return _digest(blob.encode("utf-8"))


def current_fingerprint() -> tuple[str, int]:
    """`(磁碟現況的合併指紋, 讀不到的檔案數)`，只看這個行程載入過的那些檔。"""
    files, unreadable = _read_all(_project_source_files())
    return _combine(files), unreadable


def drift_report() -> dict:
    """啟動之後，磁碟上的程式碼有沒有變過。

    `drifted` 是**三態**：`True` / `False` / `None`（判斷不出來）。`None` 出現在
    沒取過快照、或有檔案讀不到的時候。這裡刻意不把「不知道」摺成 `False`——那正是
    本專案在 `_find_all_chrome_processes`、`_load_pid`、`dashboard_server` 上各踩過
    一次的形狀：**失敗的掃描長得跟乾淨的掃描一模一樣**。

    `changed` / `added` / `removed` 只在 `drifted` 為 True 時才有意義，而且是給人
    看的診斷；不要拿它當控制流程的依據。
    """
    if _SNAPSHOT is None:
        return {"drifted": None, "why": "no snapshot taken at startup",
                "at_start": None, "now": None,
                "changed": [], "added": [], "removed": []}
    now_files, unreadable = _read_all(_project_source_files())
    changed = sorted(n for n, d in now_files.items()
                     if n in _SNAPSHOT and _SNAPSHOT[n] != d)
    added = sorted(set(now_files) - set(_SNAPSHOT))
    removed = sorted(set(_SNAPSHOT) - set(now_files))
    at_start = _combine(_SNAPSHOT)
    now = _combine(now_files)
    if unreadable or _SNAPSHOT_UNREADABLE:
        drifted = None
        why = (f"{unreadable + _SNAPSHOT_UNREADABLE} source file(s) unreadable; "
               "cannot tell")
    else:
        # **`added` 刻意不算漂移。** 這裡問的是「我正在跑的，還是不是磁碟上那一
        # 份？」——而 `added` 幾乎只有一個成因：一個本專案的模組在 `snapshot()`
        # **之後**才被 import 進來（延遲 import）。那個模組是**剛剛才從磁碟載入
        # 的**，也就是說它是最新的，正是「沒有落後」。把它算成漂移的話，任何一次
        # 延遲 import 都會讓這個回報從此永遠是 True，變成一個每輪都叫、而且叫錯的
        # 警報——會亂叫的警報最後會被人關掉（`test_language` 記過同一個教訓）。
        #
        # 真正代表「我落後了」的只有兩種：`changed`（同一個檔案的內容變了）與
        # `removed`（我載入過的檔案在磁碟上不見了，例如模組被改名）。
        #
        # 這條也讓「呼叫端必須保證 snapshot() 之後不得再有本專案的 import」這個
        # 外部約束消失——那種約束沒有東西守得住，遲早被一個看起來人畜無害的延遲
        # import 破壞。
        drifted = bool(changed or removed)
        why = "" if not drifted else (
            f"{len(changed)} changed, {len(removed)} removed since start")
    return {"drifted": drifted, "why": why, "at_start": at_start, "now": now,
            "changed": changed, "added": added, "removed": removed}


def describe() -> str:
    """一行摘要，給啟動橫幅與狀態指令用。

    **這個字串只放檔名與十六進位摘要**，不放主機路徑、不放例外文字——它會被送到
    對話平台，所以受 CLAUDE.md 保密規則 Layer 1 約束（`PACKAGE_ROOT` 相對路徑，
    不是絕對路徑）。
    """
    report = drift_report()
    if report["drifted"] is None:
        base = report["at_start"] or "?"
        return f"code {base} (drift unknown: {report['why']})"
    if not report["drifted"]:
        return f"code {report['at_start']} (matches disk)"
    # 只列真正代表「落後」的那兩類；`added` 不算漂移（見 `drift_report`），列出來
    # 只會讓人以為那個檔案有問題。
    names = report["changed"] + report["removed"]
    head = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return (f"code {report['at_start']} at start, disk now {report['now']} "
            f"— {report['why']}: {head}")
