"""Cross-run resume checkpoint for the webrunners.

When a batch is interrupted mid-character (`!stop`, crash, Chrome respawn),
the four todo files are NOT popped (the pop only fires on a near-complete
character), so the first pending pair on the next run is exactly the one that
was in progress. Without help the next run allocates a fresh numbered output
folder (`<name>_2`) and regenerates that character from image 1, throwing away
the partial progress already on disk.

This module persists a tiny JSON checkpoint of the *currently in-progress*
pair so the next run can recognise "same first pair" and resume into the same
output folder, generating only the images still missing.

Design notes:

- File-only state (per CLAUDE.md module boundaries the bot and webrunner never
  import each other; this passive helper is imported by both webrunner
  variants — that is allowed). `webrunner_progress.json` is gitignored like
  `webrunner.pid`.
- Written ONCE at each character's start with the full pair identity (so an
  edited queue does NOT falsely resume), the chosen output folder name, and
  the target count. Never updated mid-character — the output folder itself is
  the source of truth for "how many done so far".
- Cleared the moment a character is popped (completed). That clear is what
  makes adjacent identical prompts safe: otherwise a just-finished pair could
  masquerade as the next pair's in-progress checkpoint.
- Every function swallows its own I/O / parse errors and degrades to "no
  resume" — a corrupt checkpoint must never crash or stall a run.

Keep this the single source of truth for both `webrunner_novelai.py` and
`webrunner_je_only.py`; do not fork the logic back into the webrunners.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_FILE = _PROJECT_ROOT / "webrunner_progress.json"
# Sibling temp written then `os.replace`d onto PROGRESS_FILE — atomic on
# Windows/POSIX when on the same filesystem (it is: same dir, repo root).
# A sudden kill mid-write can therefore never leave a truncated / 0-byte
# JSON that `read_progress` would reject → None → no resume.
# `atomic_write_text` 自己從目標路徑推出 `<name>.tmp`，所以這個常數不再被寫入
# 路徑使用；留著是因為它就是那個推導結果的名字，測試以它斷言「.tmp 已被移除」。
_PROGRESS_TMP = PROGRESS_FILE.with_suffix(".json.tmp")

# Image filenames are `<character_name>_<index:04d>_<YYYYmmdd>_<HHMMSS>.png`.
# Match the trailing `_<index>_<8 digits>_<6 digits>.png` so a character name
# that itself contains underscores / digits can't confuse the index parse.
# 副檔名**不分大小寫**，要跟 `folder_image_stats` 的檔案過濾
# （`p.suffix.lower() == ".png"`）一致。兩邊不一致的話，一個 `.PNG` 會被
# 算進張數卻抽不出編號，`next_index` 就退回「張數 ＋ 1」——那個值可能小於
# 已存在的最大編號，於是下一張圖直接覆寫掉上一輪的成果，而且沒有任何錯誤。
# 放寬只會讓 `next_index` 變大，不可能製造新的碰撞。
_INDEX_RE = re.compile(r"_(\d+)_\d{8}_\d{6}\.png$", re.IGNORECASE)


def atomic_write_text(path: Path, text: str) -> bool:
    """同目錄 temp → `os.replace` 寫入 `path`；成功回 True。Best-effort，不丟例外。

    這是 webrunner 側的通用版本（`_atomic_write` 只服務本檔的檢查點）。跨行程
    檔案一律走這條——半寫入的檔案不會讓讀取端崩潰，而是**讀成別的內容**，那種
    錯誤沒有人看得出來。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as error:
        print(f"atomic_write_text({path.name}) failed: {error!r}",
              file=sys.stderr)
        # 清掉殘留的 .tmp（os.replace 成功會自動移除，失敗才需要善後）。
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _atomic_write(payload: dict) -> None:
    """Atomically replace the checkpoint with `payload`. Writes a sibling temp
    then `os.replace`s it onto the target so a kill mid-write can never leave a
    half-written file. Best-effort; never raises."""
    atomic_write_text(
        PROGRESS_FILE,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_progress(prompt: str, char1: str, char2: str, undesired: str,
                   folder: str, target: int) -> None:
    """Record the in-progress pair at character start. `saved` starts at 0 and
    is bumped live by `update_saved`. Best-effort; never raises."""
    _atomic_write({
        "prompt": prompt,
        "char1": char1,
        "char2": char2,
        "undesired": undesired,
        "folder": folder,
        "target": target,
        "saved": 0,
    })


def update_saved(count: int) -> None:
    """Atomically update JUST the `saved` count in the existing checkpoint,
    preserving the pair fields / folder / target written at character start.

    Called after every successfully downloaded image so the on-disk checkpoint
    always reflects the true current count — a backstop for `read_progress`
    callers if the output-folder listing is momentarily off (e.g. an antivirus
    holding a handle right after a sudden kill). If the checkpoint is missing or
    corrupt it no-ops; never raises."""
    progress = read_progress()
    if not progress:
        return
    progress["saved"] = count
    _atomic_write(progress)


def read_progress() -> dict | None:
    """Return the checkpoint dict, or None if absent / unreadable / malformed."""
    try:
        text = PROGRESS_FILE.read_text(encoding="utf-8")
    # 解碼失敗＝檢查點不可用，與讀不到同一個結論（回 None ＝ 從頭開始），
    # 不是「讓呼叫端炸掉」。
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(text or "null")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def clear_progress() -> None:
    """Delete the checkpoint. Best-effort; never raises."""
    try:
        PROGRESS_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"_run_progress: clear failed: {error!r}", file=sys.stderr)


# 決定「同一個配對」的四個身分欄位。`matches` / `mismatch_fields` /
# `diagnose_mismatch` 全部走這一份，順序也是對外顯示的順序。
IDENTITY_FIELDS = ("prompt", "char1", "char2", "undesired")


def mismatch_fields(progress: dict | None, prompt: str, char1: str,
                    char2: str, undesired: str) -> list[str]:
    """回傳「檢查點與目前這個配對不一致」的欄位**名稱**清單（空 = 完全一致）。

    只回名稱、不回內容：呼叫端要把它寫進 `events.ndjson` 這種跨行程、可能被
    轉貼出去的地方，欄位內容是提示詞全文，沒必要跟著跑。人看的 stored/current
    細節走 `diagnose_mismatch` 進 log。

    `progress` 為 falsy 時回空清單（沒有檢查點 ≠ 欄位不一致）——所以 `matches`
    必須自己先擋掉那個情況，不能單看這個回傳值。"""
    if not progress:
        return []
    current = {"prompt": prompt, "char1": char1,
               "char2": char2, "undesired": undesired}
    return [name for name in IDENTITY_FIELDS
            if progress.get(name) != current[name]]


def matches(progress: dict | None, prompt: str, char1: str, char2: str,
            undesired: str) -> bool:
    """True when `progress` describes exactly this pair (all four fields). An
    edited queue therefore does NOT resume — it starts the character fresh."""
    if not progress:
        return False
    return not mismatch_fields(progress, prompt, char1, char2, undesired)


# `folder` 欄位裡不該出現的字元。三個都要擋，而且要**自己列**不能靠
# `Path(name).name`：POSIX 上反斜線不是分隔符，`Path("a\b").name` 會原樣回
# `"a\b"`，於是一個 Windows 風格的路徑在 POSIX 測試機上就這樣溜過去。
_FOLDER_SEPARATORS = ("/", "\\", ":")


def _is_unsafe_folder_component(name: str) -> bool:
    """`name` 能不能安全地接到 `output_root` 底下當成**單一層**資料夾名。

    這是本專案這條規則的**第四份實作**，前三份在 `discord_bot`
    （`_is_unsafe_folder_name`）與 `_webrunner_shared`
    （`_is_safe_folder_component` / `_is_single_path_component`）。四份都在，是
    因為模組邊界，不是因為沒人注意到：**`_webrunner_shared` 自己 import
    `_run_progress`**（`_webrunner_shared.py:40`），所以反過來 import 就是循環。
    這一句是實測的，不是架構推論——去那一行看得到。

    ⚠️ 抽出成一支具名函式而不是繼續寫在 `resume_folder` 裡面，是為了讓
    `test_bot_helpers` 的接合守門**看得見**它。原本那四行 `if` 做的事一模一樣，
    但守門認的是「右運算元有沒有被餵進某支已登記的守衛」，一段內嵌的 `if` 對它
    是隱形的——於是 `output_root / name` 被列成「沒有任何紀律守住」的站點，而它
    其實是全專案守得最緊的幾個之一。**一道看不見的守衛，在守門的帳上等於沒有。**

    語意跟 `discord_bot._is_unsafe_folder_name` 對齊（回 True ＝不安全），名字
    刻意不同名：`_JOIN_GUARDS` 是用**名字**認人的，同名兩份實作等於讓其中一份
    悄悄繼承另一份的信任。
    """
    if not isinstance(name, str):
        return True
    name = name.strip()
    if not name or name in (".", ".."):
        return True
    return any(sep in name for sep in _FOLDER_SEPARATORS)


def resume_folder(progress: dict | None, output_root: Path) -> Path | None:
    """把檢查點的 `folder` 欄位翻成一個**可以安全寫入**的輸出資料夾，翻不出來
    回 None（＝這一輪不接續，從第 1 張重新產）。

    為什麼需要這一層：`matches()` 只比對四個身分欄位（prompt / char1 / char2 /
    undesired），**完全不看 `folder`**。所以一份身分欄位對得上、但 `folder`
    壞掉的檢查點會讓 `matches()` 回 True，呼叫端接著 `output_root / prog["folder"]`
    ——實測六種壞法全部中招：欄位不存在 → `KeyError`；值是 None／int／list →
    `TypeError`；值是 `"../../Windows/Temp/x"` 或 `"C:/Windows/Temp/x"` → 路徑
    直接跑到 `output/` 外面，而 `generate_loop` 會 `mkdir(parents=True)` 然後把
    整個角色的 120 張圖寫進去。前四種會把整批作業炸掉（例外從 `run_batch` 的
    起始路徑往上丟），後兩種更糟：沒有人會發現。

    這也正是本模組開頭寫的契約——「損毀的檢查點絕不能讓一輪作業崩潰或卡住」。
    契約在模組內部是守住的（每個函式都自己吞 I/O／parse 錯誤），破口在呼叫端
    直接 index 原始 dict。判斷放這裡而不是放呼叫端，是因為兩個 webrunner 變體
    共用這個模組，放這裡才只有一份。

    合格條件（任一不符就回 None）：非空字串、去掉頭尾空白後仍非空、不含
    `/` `\\` `:`、不是 `.` 或 `..`。也就是「`output/` 底下的單一層資料夾名」。

    ⚠️ **這裡原本寫著「正是 `allocate_output_dir` 產生得出來的形狀」，那句話是
    錯的，而且它錯了很久沒有人發現——2026-09-10 實測推翻。** 當時
    `_webrunner_shared.character_folder_name` 是**字元黑名單**
    （`re.sub(r'[\\\\/:*?"<>|]+', ...)`，沒有 `.`），所以佇列裡一行 `..` 會原封
    不動變成資料夾名，`allocate_output_dir("..", t)` 直接回傳 `output/..`，
    `.resolve()` 就是 `PROJECT_ROOT`。也就是說：**有人針對檢查點這條路想過
    `..`，然後對佇列那條路寫下了一個沒有驗證的假設，還把它當成理由。**

    今天那句話是真的了，但真的的**理由**要寫清楚，不能再當成天然性質：
    `character_folder_name` 已改成「先替換、再用
    `_is_safe_folder_component` 做白名單斷言，不合格退回 `"character"`」，
    所以那個投影的值域被保證是單一層相對元件。這一支仍然**獨立**檢查，不靠
    上面那個保證——它讀的是磁碟上的 JSON，可能是手改的、也可能是舊版寫的。
    兩道守衛是刻意重複的，不要因為「投影已經保證了」就把這裡拿掉。
    """
    if not progress:
        return None
    name = progress.get("folder")
    if not isinstance(name, str):
        return None
    name = name.strip()
    # ⚠️ 守衛呼叫要放在**最後一次重新綁定 `name` 之後**。接合守門不做資料流分析：
    # 它只記「這個名字被餵進過某支守衛」，所以 `guard(name); name = 別的東西;
    # root / name` 在它眼裡是綠的。這裡先 `strip()` 再檢查，就沒有那個縫。
    if _is_unsafe_folder_component(name):
        return None
    return output_root / name


def diagnose_mismatch(progress: dict | None, prompt: str, char1: str,
                      char2: str, undesired: str) -> list[str]:
    """Return one human-readable line per diverging field between the stored
    checkpoint and the current first pair. Empty list when `progress` is falsy
    or every field matches. Pure diagnostic — does NOT change the resume
    decision (a genuinely different pair must still start fresh, or its images
    would mix into the wrong folder). Exposes WHY a resume was skipped (queue
    edit? fallback flip? stray whitespace / NBSP?) in the user's environment."""
    if not progress:
        return []
    current_by_name = {"prompt": prompt, "char1": char1,
                       "char2": char2, "undesired": undesired}
    lines: list[str] = []
    for name in mismatch_fields(progress, prompt, char1, char2, undesired):
        stored = progress.get(name)
        current = current_by_name[name]
        # `read_progress` only validates that the TOP level is a dict — the
        # field values are whatever the (possibly hand-edited / corrupted)
        # JSON held. A numeric or list value would make the old
        # `(stored or '')[:60]` raise TypeError ('int' is not
        # subscriptable) from inside `run_batch`'s startup path, breaking
        # this module's "never raises, degrade to no-resume" contract for
        # what is only a diagnostic line. Stringify before slicing.
        lines.append(
            f"{name}: stored={str(stored or '')[:60]!r} "
            f"current={str(current or '')[:60]!r}"
        )
    return lines


def folder_image_stats(folder: Path) -> tuple[int, int]:
    """Return `(existing_png_count, next_index)` for an output folder.

    `next_index` is one past the highest `_NNNN_` index already present (so
    resumed images keep climbing instead of colliding with gaps left by failed
    images last run); falls back to `count + 1` when nothing parses. A missing
    folder yields `(0, 1)`."""
    try:
        pngs = [p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() == ".png"]
    except OSError:
        return (0, 1)
    count = len(pngs)
    max_idx = 0
    for p in pngs:
        m = _INDEX_RE.search(p.name)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    next_index = (max_idx + 1) if max_idx else (count + 1)
    return (count, next_index)
