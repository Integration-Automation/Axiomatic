"""外部化的 bot 端 prompt 文字載入器（passive、stdlib-only，仿 `_bot_config.py`）。

把原本寫死在程式碼裡的長 prompt 字串（Dorossi 人設系統提示、自走迴圈各段守則／
提示、`@bot generate` 的預設畫風後綴）搬到版本庫根目錄下的 `bot_prompts/`，一個
prompt 一個檔，於**開機（import 時）讀取一次**。缺檔／空白／讀取出錯一律回退到
呼叫端傳入的內建預設值，所以 fresh clone 即使 `bot_prompts/` 不完整也一定能啟動。

哨符處理採「檔案內佔位符」：檔案裡寫 `{sentinel}` / `{open_sentinel}`，載入後由呼叫
端傳入 `replacements` 把佔位符 `.replace()` 成實際哨符常數——哨符字串仍單一來源在
程式碼裡（改哨符不必動這些檔）。用**明確 replace，不用 str.format**：人設文字含全形
括號與可能的 `{}`，`str.format` 會炸。

檔尾換行規則（重要）：載入後只做 `.rstrip("\n")`——去掉編輯器自動補的檔尾換行，但
**不動任何其他空白**。這是因為 `generate_suffix.txt` 結尾的 `", "`（含尾端空白）必須
逐字保留；散文類 prompt 不依賴尾端空白，用同一規則即可。相對地，開頭的換行是語意的
一部分（FIRST_SUFFIX／VERIFY／TOOLING／SELFJUDGE 開頭真的有兩個空行），`rstrip("\n")`
只去尾端、不動開頭，故開頭換行照樣保留。讀取採 universal newline（`\r\n` → `\n`），
所以 Windows 檢出 CRLF 也不影響比對。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_PROMPTS_DIR = _PROJECT_ROOT / "bot_prompts"


def load_prompt(
    filename: str,
    default: str,
    *,
    replacements: "dict[str, str] | None" = None,
) -> str:
    """讀 `BOT_PROMPTS_DIR/filename` 並回傳其文字；讀不到／空白／任何 IOError 都回退
    到 `default` 並印一行 stderr 警告（帶檔名），**絕不 raise**。

    成功讀到後：先 `.rstrip("\\n")`（見模組 docstring 的檔尾換行規則），再套用
    `replacements`——逐一 `text = text.replace("{" + k + "}", v)`（明確 replace，
    非 `str.format`）。`replacements` 通常用來把 `{sentinel}` / `{open_sentinel}`
    佔位符換回程式內的哨符常數。
    """
    path = BOT_PROMPTS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - passive loader must never raise
        print(
            f"[_bot_prompts] 無法讀取 {filename}，改用內建預設值：{exc!r}",
            file=sys.stderr,
        )
        return default
    if not text.strip():
        print(
            f"[_bot_prompts] {filename} 內容為空，改用內建預設值。",
            file=sys.stderr,
        )
        return default
    text = text.rstrip("\n")
    if replacements:
        for key, value in replacements.items():
            text = text.replace("{" + key + "}", value)
    return text
