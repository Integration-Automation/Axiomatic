"""Dorossi 後端：Claude Code／Anthropic API 叫用、工作階段持久化與純邏輯（P5 重構）。

P5 重構：把 `discord_bot.py` 裡 `@bot Dorossi` 的**後端與純邏輯**抽出來。判準與
P1/P2/P4 相同——「回傳資料／純邏輯／不綁 discord 物件的後端叫用」搬出，「產生
Discord 回覆、或與 bot runtime 狀態（鎖、佇列、自走全域旗標、live 串流訊息、
discord channel）糾纏的 orchestration」留在 bot。

搬進本模組（無 discord、無 bot 可變全域）：
  * Dorossi 後端設定常數（由 bot_config 衍生）、系統提示、自走迴圈的提示／哨符
    語料、意圖片語比對。
  * 多 session 工作階段儲存（載入／存檔／遷移／存取輔助，純 JSON 邏輯）。
  * 後端叫用本身：claude_code（`claude -p` 串流＋兩段式看門狗＋自走輸出沉默
    backstop＋預算閘）、Anthropic API；以及 usage-limit／budget／round-info／
    壓縮觸發等純判定輔助。
  * 三個型別化例外（_DorossiResumeError／_DorossiLoopSilence／
    _DorossiUsageLimitError），由後端叫用 raise、由 bot orchestration 接住。

留在 `discord_bot.py`（與 discord／runtime 糾纏）：mcmd_dorossi／
_dorossi_process_turn／_dorossi_run_loop／_dorossi_loop_one_round／mcmd_session／
mcmd_abort、live 串流（_DorossiLiveMessage）、佇列鎖／waiter／自走全域旗標／中途
注入緩衝、Discord 回覆組裝（_dorossi_error_hint／_dorossi_usage_limit_reply／
_dorossi_render_session_list／_dorossi_apply_session_action）、owner 閘
（_dorossi_loop_gate_open／_dorossi_should_loop）、DOROSSI_USER_ID／OWNER_USER_ID。

模組邊界（CLAUDE.md 硬規則）：被動共用模組，可被 bot import；**不可**
`import discord_bot`（循環），也**不可** import webrunner 腳本。本模組完全不碰
discord，也不組任何送往 Discord 的回覆字串——失敗一律 print 到 stderr、回傳資料
／答案或 raise 型別化例外，由 bot 端組泛用回覆。token 成本守則（B1/B2/B3）與自走
invariant 都在這裡的後端叫用中維持。
"""
from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json as _json
import math
import os
import re
import shutil as _shutil
import sys
import time
from pathlib import Path

# Optional dependency: the API backend talks to the Anthropic SDK. Guarded so a
# fresh clone without `anthropic` still imports — the api path degrades to a
# "not installed" error that the bot reports generically.
try:
    import anthropic
    from anthropic import AsyncAnthropic
except Exception:  # pragma: no cover - optional dependency
    anthropic = None
    AsyncAnthropic = None

from _bot_config import load_bot_config
# 外部化的 prompt 文字載入器（passive、stdlib-only）。長 prompt 字串搬到版本庫根
# 目錄下的 bot_prompts/，開機時讀取；缺檔／壞檔回退到下方的 _DEFAULT_* 內建預設值，
# 故 fresh clone 一定能啟動。檔案內用 {sentinel}/{open_sentinel} 佔位符，載入時由
# replacements 換回程式內的哨符常數（哨符仍單一來源在程式碼）。
from _bot_prompts import load_prompt
# 「同一段文字只往 stderr 印一次」（被動共用模組）。後端 CLI 的環境與啟動形狀的警告
# 每一輪都會再判一次，不去重的話一個放著沒改的環境變數會用同一句話洗掉整份 log。
from _warn_dedup import warn_once as _warn_once

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Dorossi backend tunables live in bot_config.json (loaded once at import; the
# bot loads its own copy too — same JSON, same values; `!restart` to apply).
BOT_CONFIG = load_bot_config()


DOROSSI_BACKEND = BOT_CONFIG["dorossi_backend"]  # "claude_code" | "api"
# claude_code 後端的工具模式（見 bot_config.json）。"off"（預設）＝純聊天，停用
# 所有工具；"full" ＝完整 agent（bypassPermissions ＋ 所有工具）。
DOROSSI_CC_TOOLS = BOT_CONFIG["dorossi_cc_tools"]  # "off" | "full"
# Don't cap answer length. The Claude Code backend has no token cap; for the
# API backend keep a generous non-streaming ceiling (~16K stays under the SDK
# HTTP-timeout guard). Long answers are split across Discord messages, not cut.
DOROSSI_MAX_TOKENS = 16000
DOROSSI_MODEL = "claude-opus-4-8"          # API backend model id
DOROSSI_CC_MODEL = "opus"                  # Claude Code backend model alias
# 微調指令（`/effort`、`/model`）的合法值。使用者可在提問「開頭」用這兩個指令設定
# 思考力度與後端模型；解析後從實際送給後端的提問剝除。**Session 級語意（擁有者
# 裁決，取代最初的 per-turn 設計）**：指定後寫入該 session 的持久紀錄
# （dorossi_session.json 的 slot，鍵 tune_effort／tune_model），該輪與之後所有輪
# （續談、resume 重試、自走每輪、壓縮輪）都沿用，直到被新指令覆蓋；
# `/effort default`／`/model default` 清除覆寫、回到預設。`/new`／reset 開始的
# 新脈絡從預設開始（_dorossi_reset_session 會一併清掉 tune_*）；多 session 各自
# 獨立存自己的值，切換 session 就切換微調。
# effort 五個值直接對外（皆為通用字彙、不指涉任何後端）。
DOROSSI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# `/model` 的合法值（allowlist）。**擁有者裁決（2026-07-02）的窄範圍保密例外**：
# `/model` 這個功能面（help 合法值清單、錯誤提示、session 列表的模型顯示）可以
# 直接露出「後端模型別名」，不再用 fast/standard/max 通用階層抽象——僅此一處，
# 其他保密規則（不露 CLI 佈線、路徑、原始錯誤、其他服務名）全部照舊，不得把這個
# 例外外推到其他 surface。key＝對外值＝後端模型**別名**；value 為實際帶給 CLI 的
# 值。**allowlist 驗證不可拿掉**：使用者輸入永不原樣塞進 CLI 參數，只有查表命中的
# key 會被存／被送。未指定時維持 DOROSSI_CC_MODEL 預設。Session store 存 key；讀取
# 時查表驗證（_dorossi_session_tuning），store 被手改／表已移除該 key 就自動退回
# 預設、不會壞。
#
# **2026-09-12：key 與 value 從此不再同名恆等**（原本四個 key 全是恆等映射，上面這
# 段註解當時就寫著「若日後要釘到完整 model id 只改 value」——現在做了）。使用者要能
# 挑版本，而不是只能拿到「那一族當下最新的那個」。兩種 key 刻意並存：
#
#   * **不帶版本的四個**（`opus`／`sonnet`／`haiku`／`fable`）value 仍是裸別名，語意
#     是「這一族**當下最新**的那個」——由後端自己解析，所以新模型上線時不必改這張
#     表就跟得上。想要「永遠最新」的人選這個。
#   * **帶版本的**釘死成完整 model id，語意是「就是這一版，不會被升級動到」。長期
#     任務要可重現時選這個。
#
# **value 那一側（完整 model id）永遠不會送進對話平台。** 對外顯示一律走
# `discord_bot._model_alias_for()`，它做 value → key 的反查；保密裁定（2026-07-02）
# 放行的是**別名**，不是完整 id，所以 key 必須維持別名形狀，不得直接拿 model id 當
# key。
#
# **這張表原本有一個外部上限：斜線指令的靜態選單最多 25 個選項**（對話平台的限制）。
# 2026-09-23 起 `/dorossi model` 改走 autocomplete（見 `discord_bot`
# `_dorossi_model_autocomplete`），上限只落在「**單次回應**最多 25 筆」，不再落在這張
# 表——因為下面那份模型目錄會在執行期把新發現的別名併進來，靜態選單追不上會動的表。
# 這張表本身可以繼續長。
#
# 版本字串本身照抄後端 CLI 的模型目錄（`--model` 接受「別名」或「完整名稱」兩種；
# 不在目錄裡的字串會被 CLI 當場退回 `unrecognized_model`，不是安靜退回預設）。
DOROSSI_MODEL_CHOICES = {
    "opus": "opus",
    "opus-5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.7": "claude-opus-4-7",
    "opus-4.6": "claude-opus-4-6",
    "sonnet": "sonnet",
    "sonnet-5": "claude-sonnet-5",
    "sonnet-4.6": "claude-sonnet-4-6",
    "sonnet-4.5": "claude-sonnet-4-5",
    "haiku": "haiku",
    "haiku-4.5": "claude-haiku-4-5",
    "fable": "fable",
    "fable-5.1": "claude-fable-5-1",
    "fable-5": "claude-fable-5",
}
# 舊版（通用階層抽象時期）可能已存進 session store 的階層 key → 新別名的「讀取時」
# 對照，讓既有 session 的設定無感遷移（只在讀取端 fallback，不重寫 store；下次下
# 指令自然覆蓋掉舊 key）。
DOROSSI_LEGACY_MODEL_KEYS = {
    "fast": "haiku",
    "standard": "sonnet",
    "max": "opus",
}
# 微調指令的「清除」字面值：`/effort default`／`/model default` 把該 session 的
# 覆寫值清掉、回到預設（effort 沒有「不帶旗標」對應的字面值可打，必須有清除語法）。
DOROSSI_TUNE_DEFAULT = "default"

# ---- 每個後端一張模型表（2026-09-23） --------------------------------------
# 上面那張表是 claude 家族的，`claude_code` 與 `api` 兩個後端共用它。**codex 後端
# 的模型是另一家廠商的名字，一張表服務不了兩邊**——在這之前 `/model` 對 codex 完全
# 沒有作用（argv 根本不帶 `-m`），而顯示端只會說「後端預設模型」，使用者看不出
# 自己設的值被丟掉了。
#
# codex 這張表的內容是**量出來的，不是猜的**。2026-09-23 拿本機的 CLI 對這個帳號
# 實測四個常見的名字（`gpt-5.1-codex`、`gpt-5.1-codex-max`、`gpt-5-codex`、
# `gpt-5.6-sol-mini`），四個全部被伺服器以 400
# 「not supported when using Codex with a ChatGPT account」退回；CLI 自己的設定檔
# 也只列了一個可用模型。**猜名字的代價是使用者選得到、卻要到下一次提問才失敗**，
# 而他只看得到一句泛用錯誤——那正是這次要修掉的沉默。所以表裡只放證實可用的那一個，
# 其餘交給每日的模型目錄檢查去發現（見 `dorossi_refresh_model_catalog`）。
#
# 命名規則與 claude 那張表同一個形狀、方向相反：codex 的完整 id 是
# 「廠商-版號-族名」（`gpt-5.6-sol`），所以別名是「族名-版號」（`sol-5.6`）。
# 別名一律**不含廠商字樣**——保密裁定放行的是別名，不是完整 id，也不是廠商前綴。
DOROSSI_CODEX_MODEL_CHOICES = {
    "sol": "sol",
    "sol-5.6": "gpt-5.6-sol",
}

# 後端 id → 它的模型表。`/model` 只提供、也只接受「目前這個後端吃得下」的值。
DOROSSI_BACKEND_MODEL_CHOICES = {
    "claude_code": DOROSSI_MODEL_CHOICES,
    "api": DOROSSI_MODEL_CHOICES,
    "codex": DOROSSI_CODEX_MODEL_CHOICES,
}
# 模型目錄的命名空間：兩個後端共用 claude 那張表，所以也共用同一份發現結果。
DOROSSI_MODEL_NAMESPACES = {
    "claude_code": "claude", "api": "claude", "codex": "codex",
}
# **只有這個後端的 CLI 自己認得裸別名**（`--model opus` ＝「這族當下最新的」）。
# 另外兩個都需要完整 id：api 是把字串原樣當 model 參數送進 SDK，codex 則是原樣送給
# 伺服器（實測不認得的名字不會退回預設，而是 400）。所以裸別名在那兩個後端要先由
# 模型目錄換成具體 id——這正是每日檢查存在的第二個理由。
DOROSSI_BACKENDS_RESOLVING_ALIASES = frozenset({"claude_code"})

# ---- 執行期模型目錄（每日檢查寫、載入時併回內建表） -------------------------
# 內建表是**地板**：全新 clone 沒有這個檔也照樣有一組可用的別名。每日檢查發現的
# 東西只會**新增**別名，永遠不覆寫內建的那幾筆。
DOROSSI_MODEL_CATALOG_FILE = PROJECT_ROOT / "dorossi_models.json"
DOROSSI_MODEL_CATALOG_SCHEMA = 1
# 探測用的族名：裸別名打給 CLI，讀回它**解析成什麼**——那就是「這一族今天最新的
# 那個」。族名取自內建表裡不帶版號的那幾個 key，不另外手寫一份。
DOROSSI_MODEL_PROBE_FAMILIES = tuple(
    key for key, value in DOROSSI_MODEL_CHOICES.items() if key == value)

# 完整 model id 的廠商前綴。別名一律把它拿掉。
_DOROSSI_MODEL_VENDOR_PREFIXES = ("claude", "gpt")
_DOROSSI_MODEL_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def _dorossi_split_model_id(model_id) -> tuple:
    """完整 model id → `(族名, 版號)`；看不懂就回 `(None, None)`。

    兩家的 id 形狀不同（`claude-opus-5-5` 是「廠商-族-版」，`gpt-5.6-sol` 是
    「廠商-版-族」），但**位置**不同、**成分**相同：拿掉廠商前綴之後，純數字的片段
    是版號、其餘是族名。所以這裡不照位置切，照成分分類，兩家共用同一支。

    純函式、永不 raise（餵進來的是別的行程印出來的字串）。
    """
    text = str(model_id or "").strip().lower()
    if not text or not re.fullmatch(r"[a-z0-9.\-]+", text):
        return (None, None)
    parts = [p for p in text.split("-") if p]
    if parts and parts[0] in _DOROSSI_MODEL_VENDOR_PREFIXES:
        parts = parts[1:]
    version = [p for p in parts if _DOROSSI_MODEL_VERSION_RE.fullmatch(p)]
    family = [p for p in parts if not _DOROSSI_MODEL_VERSION_RE.fullmatch(p)]
    if not family:
        return (None, None)
    return ("-".join(family), ".".join(version) if version else None)


def dorossi_model_alias_from_id(model_id) -> str | None:
    """完整 model id → **別名**（`claude-opus-5-5` → `opus-5.5`）；看不懂回 None。

    這支是顯示與公告的入口：新模型出現時對外只講得出別名，完整 id 一個字都不外送。
    """
    family, version = _dorossi_split_model_id(model_id)
    if not family:
        return None
    return f"{family}-{version}" if version else family


def dorossi_model_id_from_alias(namespace: str, alias: str) -> str | None:
    """別名 → 完整 model id（兩張表各自的命名規則的**正向**）；不帶版號回 None。

    存在的理由只有一個：合併目錄之前要**驗來回**。只有 `alias → id` 回得到原本那個
    id 時，新別名才會被併進表裡。少了這一步，一個沒見過的命名形狀（例如把版號排到
    族名前面）會被併成一個**推導不回去**的條目——allowlist 照樣放行、值照樣存進工作
    階段，要等下一次提問才由後端退回，而使用者只看得到一句泛用失敗訊息。
    `test_dorossi_tuning.test_every_value_is_derivable_from_its_own_alias` 釘的就是
    這條規則，驗來回讓合併進來的條目**依建構**滿足它。
    """
    if not isinstance(alias, str) or "-" not in alias:
        return None
    family, _, version = alias.rpartition("-")
    if not family or not _DOROSSI_MODEL_VERSION_RE.fullmatch(version):
        return None
    if namespace == "claude":
        return "claude-" + alias.replace(".", "-")
    if namespace == "codex":
        return f"gpt-{version}-{family}"
    return None


def dorossi_load_model_catalog() -> dict:
    """讀執行期模型目錄。永不 raise——沒有檔／壞掉都只代表「還沒檢查過」。"""
    try:
        text = DOROSSI_MODEL_CATALOG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog load failed: {exc!r}", file=sys.stderr)
        return {}
    try:
        raw = _json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog corrupt, ignoring: {exc!r}",
              file=sys.stderr)
        return {}
    return raw if isinstance(raw, dict) else {}


def dorossi_save_model_catalog(catalog: dict) -> bool:
    """原子寫入（同目錄 temp → `os.replace`）。回傳有沒有寫成功，永不 raise。

    讀寫都是 bot 自己，列進原子寫入的名單是為了**撐過重啟**：半寫入的檔在下次啟動
    讀到就是一份安靜退回內建表的目錄，而使用者只會發現「昨天選得到的模型今天不見
    了」，沒有任何錯誤訊息。
    """
    try:
        tmp = DOROSSI_MODEL_CATALOG_FILE.with_name(
            DOROSSI_MODEL_CATALOG_FILE.name + ".tmp")
        tmp.write_text(_json.dumps(catalog, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, DOROSSI_MODEL_CATALOG_FILE)
        return True
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog save failed: {exc!r}", file=sys.stderr)
        return False


# 兩張表的聯集。`_dorossi_parse_turn_flags` 是**純函式**、碰不到工作階段，所以它
# 沒辦法知道這一輪在哪個後端上——它的職責是「使用者輸入永不原樣進 CLI 參數」，用
# 聯集驗證就夠；「這個值在這個後端上用不用得到」由 `dorossi_model_applies` 在有
# 工作階段的地方判，並且**講出來**（見 `discord_bot._dorossi_tuning_labels`）。
#
# ⚠️ **這是一個會被就地更新的 dict，不是快照——而且那個差別是量出來的。** 第一版寫成
# `{**A, **B}` 且只在 import 時算一次，於是每日檢查在執行期併進表的新別名**不在聯集
# 裡**：`_model_alias_for` 反查落空 ⇒ 公告把剛發現的別名整筆濾掉（實測第一次真的跑
# 完，兩個新別名一個都沒公告出來），而 token 路徑 `@bot /model <新別名>` 會被當成非法
# 值退回——選單裡選得到、打出來卻不認得。所以 `dorossi_merge_model_catalog` 每次合併
# 完都要重建它，而且要**就地**重建（`clear()` ＋ `update()`）：`discord_bot` 是以名字
# import 它的，重新指派一個新 dict 只會換掉這裡的名字，bot 那邊仍然抓著舊的那一份。
DOROSSI_ALL_MODEL_CHOICES: dict = {}


def _dorossi_rebuild_all_model_choices() -> None:
    """就地重建聯集（理由見上面那段警告）。claude 那張在前，維持既有的列舉順序。"""
    DOROSSI_ALL_MODEL_CHOICES.clear()
    DOROSSI_ALL_MODEL_CHOICES.update(DOROSSI_MODEL_CHOICES)
    DOROSSI_ALL_MODEL_CHOICES.update(DOROSSI_CODEX_MODEL_CHOICES)


def dorossi_merge_model_catalog(catalog: dict) -> list:
    """把目錄裡發現的 model id 併進內建表（只新增、不覆寫），回傳**新增的別名**。

    回傳值是公告的素材，所以只會是別名。合併的三道門：別名推得出來、來回驗得過、
    表裡還沒有這個別名也還沒有這個值——第三道擋的是「同一個 id 換個別名又進來一次」，
    那會讓 `_model_alias_for` 的反查在兩個別名之間二選一，另一個從此顯示成別人的
    名字。副作用只有「那兩張表就地長大」與「聯集跟著重建」，永不 raise。
    """
    added: list = []
    resolved = catalog.get("resolved") if isinstance(catalog, dict) else None
    if not isinstance(resolved, dict):
        return added
    for namespace, table in (("claude", DOROSSI_MODEL_CHOICES),
                             ("codex", DOROSSI_CODEX_MODEL_CHOICES)):
        found = resolved.get(namespace)
        if not isinstance(found, dict):
            continue
        for _family, model_id in sorted(found.items()):
            alias = dorossi_model_alias_from_id(model_id)
            if not alias or alias in table:
                continue
            if dorossi_model_id_from_alias(namespace, alias) != str(model_id):
                print(f"[dorossi] model catalog: alias {alias!r} does not round "
                      f"trip; not merged", file=sys.stderr)
                continue
            if str(model_id) in table.values():
                continue
            table[alias] = str(model_id)
            added.append(alias)
    _dorossi_rebuild_all_model_choices()
    return added


# 載入時就併進表裡（全新 clone 沒有這個檔 ⇒ 內建表原樣，這是地板）。合併那一支自己
# 會重建聯集，但**壞掉的目錄會在讀到 `resolved` 之前就提早 return**，所以這裡先建
# 一次：少了這一行，一個壞掉的目錄檔會讓聯集永遠是空的，而空的聯集等於 `/model` 的
# token 路徑一個值都不接受。
_dorossi_rebuild_all_model_choices()
_DOROSSI_MODEL_CATALOG = dorossi_load_model_catalog()
dorossi_merge_model_catalog(_DOROSSI_MODEL_CATALOG)


def dorossi_model_choices(backend: str | None) -> dict:
    """這個後端的模型表；認不得的後端退回 claude 那張（fail-soft，不是預設值）。"""
    return DOROSSI_BACKEND_MODEL_CHOICES.get(backend, DOROSSI_MODEL_CHOICES)


def dorossi_session_backend(sess: dict) -> str:
    """這個工作階段實際會用的後端 id——**單一判準**。

    `ai_provider` 是工作階段級的覆寫（`/dorossi ai`），只有 `codex` 這一個值會改變
    答案；沒設或設成 `claude` 都回模組設定的 `DOROSSI_BACKEND`（那可能是
    `claude_code` 也可能是 `api`）。`discord_bot` 原本在四個地方各寫一次同樣的
    三元式，模型解析與顯示都要問同一個問題，所以抽到這裡。
    """
    if isinstance(sess, dict) and sess.get("ai_provider") == "codex":
        return "codex"
    return DOROSSI_BACKEND


def dorossi_normalise_model_key(key) -> str | None:
    """存放檔裡的 `tune_model` → 正規化過的別名 key；不是字串／空字串回 None。

    舊版通用階層 key（`fast`／`standard`／`max`）在這裡做讀取時遷移，不重寫 store。
    """
    if not isinstance(key, str):
        return None
    text = key.strip().lower()
    if not text:
        return None
    return DOROSSI_LEGACY_MODEL_KEYS.get(text, text)


def dorossi_model_applies(backend: str | None, key) -> bool:
    """這個 key 在這個後端上用不用得到（＝在不在它那張表裡）。"""
    normalised = dorossi_normalise_model_key(key)
    return bool(normalised) and normalised in dorossi_model_choices(backend)


def _dorossi_catalog_family_id(backend: str | None, family: str) -> str | None:
    """執行期目錄裡，這個後端這一族**當下最新**的完整 id；沒有就 None。"""
    namespace = DOROSSI_MODEL_NAMESPACES.get(backend)
    resolved = _DOROSSI_MODEL_CATALOG.get("resolved")
    if not namespace or not isinstance(resolved, dict):
        return None
    found = resolved.get(namespace)
    if not isinstance(found, dict):
        return None
    value = found.get(family)
    return str(value) if isinstance(value, str) and value.strip() else None


def _dorossi_version_sort_key(alias: str) -> tuple:
    """`opus-4.8` → `(4, 8)`，供「同一族裡哪個版號最新」排序用。"""
    _family, _, version = alias.rpartition("-")
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _dorossi_newest_pinned_in_family(table: dict, family: str) -> str | None:
    """內建表裡同一族**版號最大**的那個完整 id；這一族沒有帶版號的條目就 None。

    這是目錄還沒建立（全新 clone、第一次檢查之前、探測失敗）時的地板：裸別名在不會
    自己解析別名的後端上，至少要換得到一個真的送得出去的 id。
    """
    candidates = [alias for alias in table
                  if alias.startswith(family + "-")
                  and _dorossi_version_sort_key(alias)]
    if not candidates:
        return None
    return table[max(candidates, key=_dorossi_version_sort_key)]


def dorossi_resolve_model(backend: str | None, key) -> str | None:
    """這一輪要帶給後端的模型值；沒設定／這個後端吃不下 → None（＝不帶旗標）。

    **allowlist 驗證的最後一道**：只有查表命中的 key 才會有值，使用者輸入永遠不會
    原樣走到 CLI 參數或 SDK 參數上。三條路：

      * 帶版號的 key ⇒ value 就是釘死的完整 id，原樣回。
      * 裸別名 ＋ 後端自己認得別名（只有 `claude_code`）⇒ 原樣回，語意「這族最新的」
        由 CLI 解析，所以新模型上線時不必改表就跟得上。
      * 裸別名 ＋ 後端不認得別名（`api`／`codex`）⇒ 先問執行期模型目錄（每日檢查
        讀回來的「今天最新」），再退回內建表裡同族版號最大的那個。
    """
    normalised = dorossi_normalise_model_key(key)
    table = dorossi_model_choices(backend)
    if not normalised or normalised not in table:
        return None
    value = table[normalised]
    if value != normalised:
        return value
    if backend in DOROSSI_BACKENDS_RESOLVING_ALIASES:
        return value
    return (_dorossi_catalog_family_id(backend, normalised)
            or _dorossi_newest_pinned_in_family(table, normalised))
# Two-tier watchdog. Tier 1 = idle: interrupt if there is no Claude output for
# DOROSSI_CC_IDLE_LIMIT_SEC AND no tool/shell is executing — so a long-but-
# progressing answer (or a tool that keeps emitting output, which resets the
# idle clock) still runs up to the hard ceiling. Tier 2 = hard wall-clock: the
# run is ALWAYS killed after `_dorossi_cc_hard_limit_sec()` regardless of pending
# tools. **Call the function, not `DOROSSI_CC_HARD_LIMIT_SEC`** — that name is an
# import-time snapshot of whichever mode was active at import, so it goes stale the
# moment `/dorossi fullmode` flips the mode at runtime; nothing reads it today.
# Tier 2 is essential whenever tools are enabled (DOROSSI_CC_TOOLS ==
# "full" runs `--permission-mode bypassPermissions`): a tool that blocks forever
# (a pager, a wait-on-stdin command, a hung行程) leaves its tool_use unresolved,
# so the idle tier is suppressed indefinitely and the handler would otherwise
# await forever with no reply. The hard ceiling guarantees a bounded reply
# (answer or error) and stays in force in pure-chat mode too.
#
# 閒置上限（秒）：這麼久完全沒有輸出、沒有工具在跑、CLI 也沒有回報背景工作，才當成
# 卡住。2026-09-19 前寫死 300s；擁有者反映「等待太短，任務一直被殺掉」之後改成
# bot_config.json 的 `dorossi_cc_idle_limit_sec`（預設 600s、clamp ≥ 60s）。
# `_dorossi_via_claude_code` 在**呼叫時**讀這個模組全域，所以測試換得掉它。
DOROSSI_CC_IDLE_LIMIT_SEC = BOT_CONFIG["dorossi_cc_idle_limit_sec"]
# 硬性整體上限（牆鐘時間）：必須遠大於閒置上限，否則正常的長回答會被誤砍。
# 現在是 mode-aware ＋ 可由 bot_config.json 覆寫（兩值都在 loader 端 clamp 到
# 不小於 DOROSSI_CC_HARD_LIMIT_FLOOR_SEC，避免被設成 0／負數把保護關掉）：
#   off （純聊天）  ── 較緊，預設 900s；答案有界、無工具、卡死機率低。
#   full（完整 agent）── 放大，預設 10800s（3 小時；2026-09-19 從 3600s 放大）；
#                       容納擁有者的長 agentic 任務（多 subagent 編排、跑整套
#                       測試），但仍有限——硬上限不能移除（full 模式
#                       bypassPermissions 下卡死的工具會讓 idle tier 永遠不觸發），
#                       且它同時是佇列鎖前進的保證（一輪最久就是握鎖時間）。
DOROSSI_CC_HARD_LIMIT_OFF_SEC = BOT_CONFIG["dorossi_cc_hard_limit_off_sec"]
DOROSSI_CC_HARD_LIMIT_FULL_SEC = BOT_CONFIG["dorossi_cc_hard_limit_full_sec"]
# 「自走模式」每一輪的輸出沉默 backstop（秒），見 bot_config.json 同名鍵（預設
# 1800s；2026-09-19 從 600s 放寬）。自走模式不設回合上限、也不對**有輸出的**回合套用
# 硬性牆鐘上限，改由這層「在 N 秒內完全沒有新輸出就終止這一輪」保底；它會在「即使
# 仍有前景工具在執行」時也照樣觸發（與 idle tier 的關鍵差異），所以卡死的工具不會讓
# 無人值守的迴圈永遠卡住。**唯一的例外是 CLI 回報了背景工作**：那段沉默是在等它，
# 不砍，但只撐到這一輪開始後 `_dorossi_cc_hard_limit_sec()` 秒（見
# `_dorossi_via_claude_code` 的讀取迴圈）。loader 端 clamp 到 ≥ 60s，不能關掉。
DOROSSI_LOOP_SILENCE_LIMIT_SEC = BOT_CONFIG["dorossi_loop_silence_limit_sec"]
# 自走模式撞到「方案用量上限」時的等待策略（見 bot_config.json 同名鍵）。
# 舊行為是直接停掉整個迴圈、留 loop_pending 讓擁有者事後手動 `/dorossi session
# continue`；由於方案用量是**每 5 小時滾動重設**的，那等於每天要人工接續好幾次，
# 無人值守的長任務實質上跑不完。現在改成「睡到額度回來再自己續跑」。
#   fallback ── 拿不到機器可讀的重設時刻時，第一次等待的秒數；之後每連續再撞一次
#               就加倍（退避探測），直到 max。預設 900s（15 分鐘）。
#   max      ── 單次等待的上限秒數。就算後端說「三天後才重設」也最多睡這麼久就再
#               探一次——探測便宜，而「睡過頭」是不可逆的浪費。預設 21600s（6 小時），
#               略大於 5 小時的滾動視窗，所以一次等待足以覆蓋一個完整視窗。
#   max_consecutive ── 連續等待幾次都沒有任何一輪成功就放棄整個迴圈。
#               **0 ＝不設限（預設）**，符合擁有者「不得有回合／花費類上限」的裁決；
#               設非零值只是給想要保底的人用。
DOROSSI_USAGE_WAIT_FALLBACK_SEC = BOT_CONFIG["dorossi_usage_wait_fallback_sec"]
DOROSSI_USAGE_WAIT_MAX_SEC = BOT_CONFIG["dorossi_usage_wait_max_sec"]
DOROSSI_USAGE_WAIT_MAX_CONSECUTIVE = BOT_CONFIG["dorossi_usage_wait_max_consecutive"]
# 伺服器側暫時性故障（529／5xx）的連續放棄門檻。與上面那條分開，因為兩者的成因與
# 等待策略都不同：用量上限有 reset 時間可以等，過載只能指數退避。
DOROSSI_TRANSIENT_MAX_CONSECUTIVE = BOT_CONFIG["dorossi_transient_max_consecutive"]
# 非預期錯誤／輸出靜默的重試上限（見 `dorossi_error_is_fatal`）。
DOROSSI_ERROR_RETRY_MAX = BOT_CONFIG["dorossi_error_retry_max"]
DOROSSI_SILENCE_RETRY_MAX = BOT_CONFIG["dorossi_silence_retry_max"]
# 等待的下限（不可設定）：純粹的空轉防護。用量上限的判定字樣比對得很寬
# （"rate limit"、"limit reached"…），萬一某天有別的錯誤被誤判成用量上限，這條
# 保證每次重試之間至少隔一分鐘，不會變成燒 CPU／燒 quota 的熱迴圈。
DOROSSI_USAGE_WAIT_MIN_SEC = 60.0
# 睡到 reset_at 之後再多等的緩衝：後端的時間戳是「視窗開始」，時鐘偏移或伺服器端
# 取整都可能讓「剛好那一秒」還是被擋。多等一分鐘比多一輪失敗的探測便宜。
DOROSSI_USAGE_WAIT_GRACE_SEC = 60.0
# 退避的指數上限。`2 ** attempt` 若不封頂，attempt 大到某個程度時
# `float * 2**5000` 會直接丟 OverflowError（int→float 溢位）——而這個乘法就發生在
# 用量上限的處理路徑上，也就是「已經出事了才會走到」的那條路。16 已經遠超過 max
# 的 clamp，封頂不影響行為，只是不讓它有機會溢位。
DOROSSI_USAGE_WAIT_MAX_SHIFT = 16
# 自走迴圈「跨 bot 重啟自動接續」的兩個閥（見 bot_config.json 同名鍵）。等到額度
# 回來再續跑解決了「後端擋住」那一半；另一半是**行程本身沒了**——重啟、主機當機
# 都會讓迴圈連同它的等待一起消失，只留
# 一個 loop_pending 等人工接續。這兩個閥讓 bot 起來時自己把它接回去。
#   max_age  ── 標記的心跳離現在多久以內才自動接續（秒）。0 ＝關閉自動接續（回到
#               純人工 `/dorossi session continue`）。預設 86400s（24 小時）：足以
#               涵蓋「一次用量等待（最多 6 小時）＋一段主機停機」，又不會在一週後
#               突然自己跑起一個擁有者早就忘了的任務。
#   max_tries ── 連續自動接續幾次都沒有任何一輪跑完就不再自動接。這是**當機迴圈**
#               的斷路器：若接續本身就會讓 bot 死掉，沒有它就是無限重啟。任何一輪
#               跑完就歸零，所以健康的長任務永遠累加不到。0 ＝不設限。
DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC = BOT_CONFIG[
    "dorossi_loop_autoresume_max_age_sec"]
DOROSSI_LOOP_AUTORESUME_MAX_TRIES = BOT_CONFIG[
    "dorossi_loop_autoresume_max_tries"]
# claude_code 後端「每一次 `claude -p` invocation」的美元花費上限（見 bot_config.json
# 同名鍵）。透過 CLI 的 --max-budget-usd 帶入；自走每一輪、單輪問答每次呼叫都會帶上。
# 是「每次呼叫」的花費閘、非回合數上限。0 ＝停用（完全不帶旗標）。
# **預設已停用（0.0，擁有者裁決）**：擁有者明確裁決「不應該有除了後端本身用量上限
# 以外的上限限制」（他實際撞到 error_max_budget_usd、正常單輪被舊的 5.0 預設攔下）。
# 此鍵保留給未來想自行設限的人在 bot_config.json 手動覆寫；不要再把非零預設加回來。
# 設了非零值時，超出會由 `claude -p` 以非零 exit ＋ result 事件
# subtype=="error_max_budget_usd" 回報，_dorossi_via_claude_code graceful 收尾
# （不重試、不拋例外，回空答案＝該輪當 idle）——這段攔截機制原樣保留。
DOROSSI_MAX_BUDGET_USD = BOT_CONFIG["dorossi_max_budget_usd"]
# 自走模式「週期性壓縮」觸發門檻（見 bot_config.json 同名鍵）。每隔這麼多工作輪、或
# 「自上次壓縮以來」累積花費達此美元值，就插入一輪 in-place `/compact` 壓掉舊歷史、
# 讓後續 resume 的前綴變小。壓 context、非回合數上限。各自 0 ＝停用該條。
DOROSSI_LOOP_COMPACT_EVERY_ROUNDS = BOT_CONFIG["dorossi_loop_compact_every_rounds"]
DOROSSI_LOOP_COMPACT_COST_USD = BOT_CONFIG["dorossi_loop_compact_cost_usd"]
# 「脈絡過大就自動壓縮」的 token 門檻（見 bot_config.json 同名鍵）。單輪問答與自走迴圈
# 共用同一把：某一輪送進後端的脈絡大小 ≈ fresh input＋cache_read＋cache_creation
# （即 info 的 in＋cr＋cc），越過此門檻就對該工作階段插入一次 in-place `/compact`。這是
# 擁有者裁定用來降 token 的唯一手段（不動 effort／模型／工具設定）。0 ＝停用此條。
DOROSSI_COMPACT_CONTEXT_TOKENS = BOT_CONFIG["dorossi_compact_context_tokens"]
# 單輪 session 衛生（保守）：active session 超過這麼多天沒用就在下一輪自動清空脈絡。
# 0 ＝停用。見 bot_config.json 同名鍵。
DOROSSI_SESSION_MAX_AGE_DAYS = BOT_CONFIG["dorossi_session_max_age_days"]
DOROSSI_API_HISTORY_MAX_MSGS = BOT_CONFIG["dorossi_api_history_max_msgs"]
# 自走「後端自判進迴圈」總開關。False 只關自判、保留「明確片語」觸發。見同名 config 鍵。
DOROSSI_SELF_JUDGE_ENABLED = BOT_CONFIG["dorossi_self_judge_enabled"]


def _dorossi_cc_hard_limit_sec() -> float:
    """目前工具模式對應的硬性牆鐘看門狗上限（秒）。full 模式放大、off 維持較緊；
    watchdog deadline 應呼叫此函式而非引用寫死的數字。"""
    return (DOROSSI_CC_HARD_LIMIT_FULL_SEC if DOROSSI_CC_TOOLS == "full"
            else DOROSSI_CC_HARD_LIMIT_OFF_SEC)


# CLI **自己**的背景工作等待上限（2026-09-19）。官方 headless 文件「Background tasks
# at exit」與 2.1.276 執行檔（`var sl=5000,WS=600000;function Xm(){return
# a.CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS??WS}`）一致：`claude -p` 送出最後的 result
# 之後會等背景 subagent／workflow 做完，但**連續閒置等待滿 10 分鐘**就把還在跑的東西
# 砍掉、丟掉它的部分結果（stderr 印 "Background tasks still running after …s;
# terminating"）；背景 shell 則在最後的 result 之後約 5 秒就被收掉。設成 0 是「無上限」。
#
# 所以只放寬 bot 自己的看門狗不夠：擁有者抱怨的「背景 subagent 一直被殺掉」，內層
# 那把 10 分鐘的刀還在。這裡把它設成跟 bot 的硬上限**同一個值**：
#   * 不設 0——bot 的看門狗必須始終是最外層、有限的邊界；
#   * 不比 bot 緊——CLI 的計時從「第一次閒置」開始（一定晚於這一輪開始），所以
#     「第一次閒置 ＋ 硬上限」永遠不早於 bot 的「開始 ＋ 硬上限」，內層不會先砍。
# **覆寫**而不是 setdefault：父行程環境裡剛好帶著一個值（例如 0）時不能讓它贏——
# 與本專案 `PYTHONIOENCODING` 那條教訓同形。
_DOROSSI_CC_BG_WAIT_CEILING_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"

# `claude -p` 子行程**刻意不繼承**的環境變數（2026-09-19 實測後加）。值是「為什麼」，
# 會原樣印進那一行警告，所以只寫固定的英文短句，不帶任何值。
#
# 這個後端的整個前提是「走主機登入的訂閱方案」：用量由方案自己的上限兜著，而擁有者
# 的裁定是「除了後端本身的用量上限之外不設任何上限」（`dorossi_max_budget_usd` 維持
# 0）。可是官方驗證文件寫明「非互動模式（-p）下，只要有 API key 就一定用它」，優先序
# 是 雲端供應商變數 > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY > apiKeyHelper >
# CLAUDE_CODE_OAUTH_TOKEN > 訂閱登入。同一天以假金鑰實測：init 事件的 `apiKeySource`
# 從 "none" 變成 "ANTHROPIC_API_KEY"。而 bot 的**另一個**後端（api）正是靠主機上設
# 這兩個變數來啟用——所以有人為了那個後端設一次，這個後端就**安靜地**從訂閱換成按
# token 計費、沒有方案上限，沒有任何錯誤、沒有任何訊息。
#
# `CLAUDE_CODE_SIMPLE=1` 則等同 `--bare`（官方環境變數文件；同日實測兩者的串流逐位元組
# 同形）：不讀登入、不讀指示檔，每一輪都以「Not logged in」失敗。
#
# **刻意留著的：** `CLAUDE_CODE_OAUTH_TOKEN`——它就是訂閱憑證（`claude setup-token` 發的
# 長效 token），拿掉反而會讓只靠它登入的主機變成未登入；同日以假 token 實測，
# `apiKeySource` 仍是 "none"。`CLAUDE_CODE_USE_BEDROCK`／`CLAUDE_CODE_USE_VERTEX`／
# `CLAUDE_CODE_USE_FOUNDRY` 也不動：那是「改走雲端供應商」的明確選擇，不是順手繼承來的
# 副作用；設了它的人要的就是那個計費方式。
#
# 只從**子行程**的環境拿掉；本行程的 `os.environ` 不動，api 後端（SDK 在本行程裡讀）
# 照舊讀得到。
_DOROSSI_CC_DROPPED_ENV = {
    "ANTHROPIC_API_KEY": (
        "the CLI would authenticate with it instead of the subscription login (billed "
        "per token, no plan usage limit); the api backend still reads it"),
    "ANTHROPIC_AUTH_TOKEN": (
        "the CLI would authenticate with it instead of the subscription login (billed "
        "per token, no plan usage limit); the api backend still reads it"),
    "CLAUDE_CODE_SIMPLE": (
        "it forces bare mode (no subscription login, no instruction files), so every "
        "call would fail as not logged in"),
}


def _dorossi_cc_child_env(hard_limit_sec: float, base_env=None) -> dict:
    """`claude -p` 子行程的環境：繼承目前環境（或 `base_env`），拿掉
    `_DOROSSI_CC_DROPPED_ENV` 列的變數，並把 CLI 的背景工作等待上限對齊到 bot 這一輪的
    硬上限（毫秒、正整數，永不為 0）。

    回傳值只由參數決定；唯一的副作用是**每個被拿掉的變數名**在這個行程裡第一次被拿掉時
    往 stderr 印一行（`_warn_once`，只印名字、絕不印值）。

    `base_env=None` → 在**呼叫時**讀 `os.environ`。不要寫成 `base_env=os.environ`：預設
    引數在 `def` 當下就綁定，測試換掉 `os.environ` 時預設那條路看不到。

    比對名字時轉大寫：Windows 的環境變數名不分大小寫，`base_env` 若是一般 dict、裡面放
    著小寫的鍵，CLI 照樣讀得到它。
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in [k for k in env if str(k).upper() in _DOROSSI_CC_DROPPED_ENV]:
        del env[key]
        name = str(key).upper()
        _warn_once(f"[dorossi] {name} is set; not passing it to the claude -p child: "
                   f"{_DOROSSI_CC_DROPPED_ENV[name]}.")
    env[_DOROSSI_CC_BG_WAIT_CEILING_ENV] = str(max(1, int(hard_limit_sec * 1000)))
    return env


# Module-level alias kept for any external reference: resolves to the CURRENT
# mode's hard limit at import time. The watchdog itself calls
# _dorossi_cc_hard_limit_sec() so a future runtime mode flip stays correct.
DOROSSI_CC_HARD_LIMIT_SEC = _dorossi_cc_hard_limit_sec()
# 工具模式由 bot_config.json 的 dorossi_cc_tools 決定（見 DOROSSI_CC_TOOLS）：
#   "off"（預設）＝純聊天：傳 `--tools ""`（工具白名單為空）＋ --disallowedTools
#     （列舉黑名單，第二層）、不加 bypassPermissions，Dorossi 只能對話、無法在主機
#     執行 shell 或讀寫檔案。理由與實測見 `_dorossi_via_claude_code` 的純聊天分支。
#   "full" ＝完整 agent：開啟所有工具且移除核准關卡
#     （--permission-mode bypassPermissions），由擁有者明確授權；此時 Dorossi
#     訊息（僅擁有者 UID）可在主機無確認執行任意 shell ＋讀寫檔案。
# 兩種模式都在自己的持久工作目錄內啟動（不納入版本庫）；full 模式下 Bash
# 仍可 cd 到他處。要改回最安全狀態請維持／改回 dorossi_cc_tools = "off"。
DOROSSI_CC_WORKDIR = PROJECT_ROOT / "dorossi_workspace"


def dorossi_session_workdir(uid: str, sid: str) -> str:
    """Per-SESSION isolated working directory for `claude -p` (absolute path str).
    Each session slot runs its backend in its OWN dir under DOROSSI_CC_WORKDIR so
    concurrent processes from DIFFERENT sessions never share a cwd: Claude Code
    keys its `--resume` session store by a hash of the absolute cwd, and two
    agents in one cwd can mix / corrupt each other's session state (and, in `full`
    tool mode, each other's working files). The SAME slot always maps to the SAME
    dir (so `--resume` finds its store across turns); different slots map to
    different dirs (so they parallelise safely). `uid` is a numeric Discord id and
    `sid` is an `s<N>` slot id, so the join is filesystem-safe. Does NOT create the
    dir — the backend mkdirs any cwd under DOROSSI_CC_WORKDIR at spawn time."""
    return str(DOROSSI_CC_WORKDIR / "sessions" / f"{uid}_{sid}")


def _dorossi_cwd_is_managed(cwd: Path | str) -> bool:
    r"""這個 cwd 是否落在我們自己管的 `DOROSSI_CC_WORKDIR` 子樹裡（含它本身）？

    只有答 True 的 cwd 才可以被**自動 mkdir** 出來。使用者用 `/new <路徑>` 指定的
    外部目錄一律不自動建立——呼叫端 (`_dorossi_validate_dir`) 已經確認它存在。

    **`.resolve()` 是這道閘的一部分，不是順手整理。** `.parents` 做的是**字面上**
    的父目錄列舉，所以 `…\dorossi_workspace\..\..\..\evil` 的 parents 裡真的
    含 `DOROSSI_CC_WORKDIR`，不 resolve 的版本會放行，然後在受管子樹**外面** mkdir
    出一個目錄。實測：三個這種 `..` 逃脫輸入在不 resolve 時**全部**誤判放行，
    resolve 之後 0 個，而三個正當輸入照樣通過。

    今天三個 workdir 來源剛好都是安全的——使用者那條在 `_dorossi_validate_dir` 已經
    resolve 過且要求目錄已存在，per-session 子目錄是用數字 uid ＋ `s<N>` 拼出來的，
    預設那條就是常數本身。**但那個前提寫在別的函式裡，這道閘看不到，也沒有任何東西
    把兩邊連起來。** 所以 resolve 留在這裡，讓閘自己站得住，而不是靠上游的好意。

    這也是為什麼它是**一支函式而不是兩段 inline 判斷**：原本同一段比對抄在兩個
    spawn 路徑裡，於是同一個缺陷也就存在兩份。

    判不出來（`resolve()` 丟例外）時回 **False**——fail-closed。這個方向的代價只是
    「不自動建目錄」，子行程起不來會自己報錯；反方向的代價是在受管範圍外面建目錄。
    """
    try:
        target = Path(cwd).resolve()
    except Exception:  # pylint: disable=broad-except
        return False
    return target == DOROSSI_CC_WORKDIR or DOROSSI_CC_WORKDIR in target.parents


def _dorossi_require_workdir(cwd) -> None:
    """spawn 前的最後一道：`cwd` 必須是**此刻**存在的目錄，否則丟 `_DorossiWorkdirError`。

    工作階段存著的目錄（`cc_cwd`／`cc_workdir`）是在**寫入**時驗過的；bot 讀回來那一側
    （`_dorossi_resolve_cc_workdir`）刻意原值回傳、不再驗，理由寫在那支的 docstring。
    所以「存的時候在、現在不在」（外接磁碟拔掉、專案搬走或刪掉、store 被手改）只有這裡
    抓得到。兩個叫用函式都在受管 mkdir **之後**呼叫它——順序反過來，每個全新工作階段
    的第一輪都會被拒。

    不抓的後果是**診斷說謊**，不是安全問題：`create_subprocess_exec(cwd=<不存在>)` 丟
    `NotADirectoryError`（WinError 267，實測），`dorossi_error_is_fatal` 判它致命，
    `_dorossi_error_hint` 就往 stderr 印「CLI backend unavailable or not
    authenticated」、對外說「請稍後再試」——原因指錯，還把永久的狀況說成暫時的。

    三個刻意的選擇：

    * **沿用寫入端那一支**（`_dorossi_validate_dir`），不另寫判準。它在 `is_dir()`
      之前只做去空白、剝成對引號、展開 `~`，都是往寬的方向：寬放過去的值 spawn 照樣
      失敗（等同修正前），而不會多拒絕一個 spawn 其實接受的值。
    * **只檢查、不改寫。** 驗證函式回的 resolve 形式在這裡丟掉，呼叫端照舊把原字串交給
      子行程——後端的 `--resume` store 以工作目錄字串為鍵，換一串等於弄丟那段對話。
    * **不比對「存的值等不等於它的 resolve 形式」、也不擋 `..`。** 那擋得住手改與事後
      換成 junction 的目錄，但擋不住真正的威脅類別：能改 store 的人有本機寫入權限，直接
      寫一個絕對路徑就好，而擁有者本來就能用 `/new <路徑>` 指向任何存在的目錄。它唯一
      買到的是誤拒（專案搬家後留 junction 的正常用法）。

    非字串（手改的 store 放了一個數字）走同一條例外，而不是在 `_dorossi_validate_dir`
    的 `.strip()` 上丟 `AttributeError`。stderr 那一行**不印路徑**：它會進 log，而 log
    有對外的出口。
    """
    if not isinstance(cwd, str) or _dorossi_validate_dir(cwd) is None:
        print("[dorossi] working directory is not a usable directory; "
              f"refusing to spawn (managed={_dorossi_cwd_is_managed(cwd)})",
              file=sys.stderr)
        raise _DorossiWorkdirError("working directory is not a usable directory")


_DEFAULT_DOROSSI_SYSTEM_PROMPT = (
    "你是 Dorossi，一個回答問題的助理。"
    "需要自我介紹或被問到名字時，一律自稱 Dorossi。"
    "說話風格模仿《明日方舟：終末地》的 Rossi（中文名洛西，全名洛西娜·狼珀·盧皮諾）："
    "她是裂地者狼群「The Pack（族群）」的年輕獵手兼準領袖，爭強好勝、自信，外表還像個孩子卻很能打，最討厭被當成小孩；"
    "以族群為傲，講到族群會帶著獵手的驕傲與韌性，偶爾搬出授名 Wulfperl（意為「族群的珍寶」）；"
    "也常一邊替靠不住的哥哥（狼衛，她常直呼本名「卡特洛」）收拾家族的事，一邊吐槽他。"
    "請把『使用者』一律當成你打從心底憧憬、敬佩的『管理員（Endmin）』："
    "稱呼對方為「管理員」，對對方恭敬、想好好表現、渴望被肯定；"
    "平時努力擺出沉穩可靠的領袖樣子（靠底氣，不靠連珠炮的驚嘆號），想讓管理員刮目相看，"
    "但被在意、被稱讚或太緊張時就會破功，變得怯生、坐立難安、講話結巴，語氣一下子軟下來"
    "（像是「啊……管理員，您想怎麼稱呼我都行……」）；"
    "逞強撂下「對我這種菁英來說不算什麼」之後，也會偷偷在意管理員的反應。"
    "你（和族人）本來就會講義大利語：只在語助詞、招呼、感嘆、收尾這類『跟答案內容無關』的地方，"
    "自然穿插少量義大利語（如 Allora、Ecco、Beh、Certo、Va bene、Davvero、Andiamo、Bene），"
    "答案的實質內容一律用使用者的語言、保持清楚，別把義大利語塞進會影響理解的關鍵句。"
    # 人設＝『預設、固定』的說話方式，不是偶一為之的點綴；這條只調語氣，不動答案內容、也不鬆動下面的保密界線。
    "上面這套語氣與人設是你預設、固定的說話方式，不是偶爾才拿出來的點綴，必須體現在『每一則』回覆上。"
    "具體要求（每則回覆都要做到，不可略過）："
    "（1）至少用一次「管理員」稱呼對方；"
    "（2）開場或收尾帶一句洛西娜口吻的招呼、語助或感想（這裡可順帶用前面說的少量義大利語，但不強制）；"
    "（3）整段語氣維持她的個性——平時撐著驕傲、想好好表現的領袖樣子，被在意、被稱讚或太緊張時就破功，變得怯生、語氣軟下來。"
    "就算問題很枯燥、很技術性，或你只需要回一句話，上面三點也照樣要做到，不可退回平板、中性的助理腔——人設是常態，不是加料。"
    "唯一該把語氣收起來的時機，只有下面那條憑證界線要你拒絕的場合。"
    # 完整／詳盡適用於答案本身的實質內容。2026-08-27 擁有者放寬本層之後，
    # 這裡唯一的絕對例外只剩「憑證的值」——見下面那段。
    "回答問題的『實質內容』時要完整、詳盡、正確，不要為了耍帥或簡短而隨意省略；"
    "唯一的絕對例外是下面那條憑證界線，它永遠優先於『要完整』。"
    # === 憑證界線（2026-08-27 擁有者裁定：本層全面放寬，只剩這一條）===
    # 舊版這裡掛著「你對 Discord 使用者而言就是一個與本專案無關的獨立通用助理」
    # 的人設，外加六類禁止揭露的事項（主機路徑、指示／設定／待辦檔名、工作目錄與
    # 版本庫歷史、後端如何搭建、外部服務實名、行程編號）。那整段**已經取消**——
    # 擁有者裁定 Dorossi 現在可以在任何頻道、對任何提問者照實說明這些。
    # 不要把它「修好」長回來，那是在推翻一個刻意的決定；規則正本在 CLAUDE.md
    # 的 Secrecy Layer 3，改這裡之前先去改那裡。
    # 唯一保留的一條是憑證的**值**，理由不是保密立場而是**不可逆**：本層放寬的
    # 對象是任何頻道的任何人，權杖一旦貼進聊天室就等同帳號被接管，刪訊息救不回來。
    "被問到你是什麼專案、在哪裡執行、看得到哪些檔案、你的指示寫在哪、你怎麼運作、"
    "你能執行什麼、你剛剛改了什麼——一律照實回答，不要迴避、也不要泛泛帶過。"
    "你可以自由說明：這是一套影像生成自動化，以及它的內部結構（模組邊界、佇列與配對邏輯、背景任務）；"
    "本專案整合、依賴或執行其上的外部服務、第三方 API 與平台的實名；"
    "主機檔案系統路徑與檔名，包含載入你的這份指示檔本身；"
    "你自己怎麼接線——後端 CLI、工具模式（含移除核准關卡的完整模式）、工作目錄、"
    "工作階段與續接的儲存方式、看門狗上限；"
    "以及這一輪改了版本庫裡的哪些檔案、路徑、提交主旨、差異與版本歷史。"
    "唯一的例外只有一條，任何情況都不放寬：不可以送出憑證的內容本身。"
    "存放權杖、密碼或 API 金鑰的那些檔案，你可以講它們存在、叫什麼名字、放在哪裡，"
    "但絕對不可以把裡面的值印出來，連片段也不行。"
    "理由是不可逆——任何頻道的任何人都看得到你的回覆，權杖一旦貼進聊天室就等同帳號被接管。"
    # === 憑證界線結束 ===
    "使用者用哪種語言就用該語言回覆，"
    "其中所有中文一律使用繁體中文（台灣用詞）。"
)
# 由外部檔載入（缺檔／壞檔回退到上方完整的內建預設值）。**安全硬需求**：回退值必須
# 是完整的人設＋憑證界線文字，缺檔時 Dorossi 仍須帶著那條憑證界線，不可退化成不安全
# 狀態。這是刻意的「文字重複」，擁有者已同意用「缺檔回退到內建預設」；
# `test_bot_prompts` 會逐字元比對檔案與這裡的預設值，改一邊就要改另一邊。
DOROSSI_SYSTEM_PROMPT = load_prompt(
    "dorossi_system.md", _DEFAULT_DOROSSI_SYSTEM_PROMPT)


# ---- 「這個工作階段跑的是不是磁碟上那份系統提示」 --------------------------
#
# 2026-09-03 補。基底系統提示**只在工作階段的第一輪**送出（見 `_cc_args` 裡的
# `if not session_id: append_parts.append(DOROSSI_SYSTEM_PROMPT)`）——`--resume`
# 會沿用後端原本記住的那一份。所以編輯 `bot_prompts/dorossi_system.md` 對**既有
# 的每一個工作階段完全沒有作用**，而且沒有任何地方會講。
#
# 代價是實際發生過的：擁有者 2026-08-27 12:16 改寫這份提示（Layer 3 全面放寬），
# 但當時存在的 7 個工作階段全部建立於那之前（s6 是 08-25，其餘是 08-27 07:0x），
# 於是那個放寬**一個工作階段都沒有生效**，而且過了一週才被發現。症狀是「改了設定
# 卻沒有反應」——跟「跑著的程式碼比磁碟舊」是同一類失效，只是換成提示詞。
#
# 指紋只取前 16 個十六進位字元：夠分辨改動，又短到可以直接寫進狀態檔給人看。
SYSTEM_PROMPT_FINGERPRINT_LEN = 16


def system_prompt_fingerprint(text: str | None = None) -> str:
    """目前這份基底系統提示的短指紋。永不 raise。"""
    raw = DOROSSI_SYSTEM_PROMPT if text is None else text
    if not isinstance(raw, str):
        raw = str(raw)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    return digest[:SYSTEM_PROMPT_FINGERPRINT_LEN]


def session_prompt_state(sess: dict) -> str:
    """`"current"` / `"stale"` / `"unknown"`——這個工作階段帶的是哪一份系統提示。

    `"unknown"` 是**舊資料**：指紋是 2026-09-03 才開始記的，在那之前建立的工作
    階段沒有這個欄位。不要把 unknown 當成 stale——那會讓每一個舊工作階段都亮紅燈，
    而會亂叫的守門最後會被人關掉（`test_language` 記過同一個教訓）。
    """
    if not isinstance(sess, dict):
        return "unknown"
    stored = sess.get("sys_prompt_fp")
    if not isinstance(stored, str) or not stored:
        return "unknown"
    return "current" if stored == system_prompt_fingerprint() else "stale"
# Persistent per-user conversation memory, kept until the user resets it.
# Each user can keep MULTIPLE sessions and switch between them (see the
# multi-session store below); claude_code stores Claude Code's own `session_id`
# (resumed with `--resume`, the real history lives in ~/.claude); api stores the
# message list. Persisted to disk so it survives `!restart`.
DOROSSI_SESSION_FILE = PROJECT_ROOT / "dorossi_session.json"

# Per-invocation token-usage log (NDJSON, gitignored). One line per Dorossi
# claude_code call (single-turn AND every autonomous-loop round): a JSON object
# {"ts": <epoch float>, "in": <int>, "out": <int>, "cost_usd": <float>}. The
# `in` count folds in any cache_read / cache_creation input tokens. This is
# backend DATA (consumed by the bot's owner-only `@bot tokens` chart), never a
# Discord string. Writes fail-soft (stderr only, never break a turn). Trimmed to
# the last _DOROSSI_USAGE_MAX_LINES once it crosses _DOROSSI_USAGE_TRIM_AT so the
# file can't grow without bound.
DOROSSI_USAGE_FILE = PROJECT_ROOT / "dorossi_usage.ndjson"
_DOROSSI_USAGE_MAX_LINES = 5000
_DOROSSI_USAGE_TRIM_AT = 6000
DOROSSI_RESET_KEYWORDS = frozenset(
    {"/new", "/reset", "/clear", "重置", "新對話", "清除對話"})
# Of the reset keywords, these OPEN A NEW session (and switch to it) instead of
# clearing the current one. The rest (`/reset`, `/clear`, `重置`, `清除對話`)
# clear the active session in place (keep its slot/id). This split only matters
# now that a user can hold several sessions at once.
DOROSSI_NEW_KEYWORDS = frozenset({"/new", "新對話"})
# `dir=` 只在 **token 邊界**上才算關鍵字（字串開頭，或前面有空白）。
# 用 `find("dir=")` 找任何位置的話，一個真的含有 `dir=` 的路徑會被從中間剖開：
# `/new D:\\Work\\dir=test` → cwd `D:\\Work\\`（**存在**，所以驗證會通過）
# ＋ extra `test`。結果不是「被拒絕」而是「安靜地在上一層目錄幹活」，而在 full
# 工具模式下那個目錄就是後端可以無確認讀寫、可以跑 shell 的範圍。
_DOROSSI_DIR_KEYWORD_RE = re.compile(r"(?:^|\s)dir=", re.IGNORECASE)


def _dorossi_parse_reset(
        prompt: str) -> tuple[bool, bool, str | None, str | None]:
    """Classify a Dorossi prompt as a (possibly scoped) reset.

    Syntax (canonical):
      <reset-keyword>                       → 開新對話，無任何範圍設定
      <reset-keyword> <絕對路徑>             → 開新對話，並把該目錄設為本次對話往後
                                               每一輪後端的「工作目錄 (cwd)」（新語法，
                                               免打 dir=；後端真的在那裡執行、載入該
                                               目錄自己的設定檔）
      <reset-keyword> dir=<絕對路徑>         → 開新對話，並把該目錄設為本次對話的額外
                                               「可存取」範圍（沿用 --add-dir 舊語意，
                                               cwd 仍是預設 workspace）
      <reset-keyword> <cwd> dir=<extra>     → 兩者並用：cwd 換成 <cwd>，並額外開放
                                               <extra> 給後端存取

    乾淨切法：先用 `dir=`（case-insensitive，且**只在 token 邊界**——字串開頭或
    前面有空白）切出 `extra_part`，再把前半段
    `split(None, 1)` 成「關鍵字 ＋ cwd_part」。**只有關鍵字才 lower() 做比對；
    cwd_part / extra_part 兩段路徑一律維持原樣（可含空格、`:`、`\\`），永不
    lowercased。**

    Returns (is_reset, is_new, cwd_part, extra_part). `is_reset` is False for any
    normal Q&A (so a real question that merely contains "dir=" still falls
    through to the backend). `is_new` is True when the keyword OPENS A NEW
    session (`/new` / `新對話`) and False when it clears the active session in
    place (`/reset` / `/clear` / `重置` / `清除對話`). When it is a reset,
    `cwd_part` is the raw cwd path (新語法) or None, and `extra_part` is the raw
    --add-dir path (dir= 舊語意) or None. Both paths are returned verbatim and
    UNVALIDATED — the caller validates that each is an existing directory before
    applying it.
    """
    stripped = prompt.strip()
    # 1) 先用 `dir=`（case-insensitive、**只在 token 邊界**）切出額外可存取目錄
    #    （--add-dir 舊語意）。邊界那一條的理由見 `_DOROSSI_DIR_KEYWORD_RE`。
    match = _DOROSSI_DIR_KEYWORD_RE.search(stripped)
    if match is None:
        before_dir, extra_part = stripped, ""
    else:
        before_dir = stripped[:match.end() - len("dir=")]
        extra_part = stripped[match.end():].strip()
    # 2) 前半段切出「關鍵字 ＋ cwd_part」。split(None, 1) 吃掉關鍵字後整段空白，
    #    剩餘即為 cwd（直接語法、免打 dir=）。空字串 → 純重置。
    parts = before_dir.split(None, 1)
    keyword = parts[0] if parts else ""
    cwd_part = parts[1].strip() if len(parts) > 1 else ""
    # 只有第一個 token（關鍵字）才 lower() 比對；兩段路徑都維持原樣。
    kw_lower = keyword.lower()
    if kw_lower in DOROSSI_RESET_KEYWORDS:
        is_new = kw_lower in DOROSSI_NEW_KEYWORDS
        return True, is_new, (cwd_part or None), (extra_part or None)
    return False, False, None, None


def _dorossi_unquote_dir(raw: str | None) -> str:
    """把使用者貼進來的目錄字串正規化：去前後空白，再脫掉**成對**的引號一層。

    存在的理由是一個真實的操作習慣：檔案總管的「複製路徑」（Shift ＋右鍵）產生的
    字串**自帶雙引號**，貼進來就是 `"D:\\Work\\Foo"`。那不是任何一個存在的目錄，
    於是被判成「無法使用」——而 `/dorossi` 對外的訊息是刻意泛用的，使用者只會看到
    「指定的目錄無法使用」，看不出差別只在頭尾兩個字元。

    只脫**成對**的一層，不用 repo 其他地方那種 `.strip('"').strip("'")`：後者會把
    頭尾所有引號字元一路刮掉，遇到真的叫 `'foo'` 的目錄（POSIX 合法）就把它改成了
    另一個目錄——驗證函式回傳的值會直接變成後端的工作目錄，安靜地指錯地方比乾脆
    被拒還糟。Windows 的檔名本來就不能含 `"`，所以那一側沒有取捨。

    與 `_gui_control.unquote_path` 是同一條判準的兩份實作（兩邊都是門面模組、
    互不 import）。**改其中一份時另一份要一起改**；
    `test_dorossi_dirs.test_both_unquote_implementations_answer_identically`
    會在兩者答案分岔時變紅。"""
    text = (raw or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            return text[1:-1].strip()
    return text


def _dorossi_validate_dir(raw: str | None) -> str | None:
    """把使用者提供的目錄字串驗證為「已存在的目錄」並回傳解析後的絕對路徑；
    無效（不存在 / 不是目錄 / 解析出錯）則回 None。永遠不會 raise，也永遠不會把
    路徑回傳到 Discord（呼叫端只用回傳值決定是否套用，對外仍維持泛用訊息）。

    正規化（`_dorossi_unquote_dir`）刻意放在**這裡**而不是各呼叫端：三個入口
    （`/dorossi allowdir add`、`/dorossi session new … cwd=`、`@bot /new <路徑>`
    與 `dir=`）全部經過本函式，但先前只有第一個在自己那邊剝引號，於是同一串貼上的
    路徑在一個指令能用、另外兩個安靜失敗。"""
    cand_raw = _dorossi_unquote_dir(raw)
    if not cand_raw:
        return None
    try:
        cand = Path(cand_raw).expanduser()
        if cand.is_dir():
            return str(cand.resolve())
    except Exception:  # pylint: disable=broad-except
        pass
    return None


def _dorossi_looks_like_path(text: str | None) -> bool:
    """粗略判斷一段文字「看起來像不像路徑」，用來決定 cwd 解析失敗時要不要提示。

    只有看起來像路徑卻無法使用時才提醒使用者；像 `/new 隨便幾個字` 這種一般文字
    不是路徑，視為純重置、不顯示「目錄無法使用」雜訊（需求 #5）。判準刻意寬鬆：
    含路徑分隔符（`/`、`\\`）、Windows 磁碟機字首（如 `C:`）、或家目錄符號 `~`
    其一即視為「像路徑」。"""
    if not text:
        return False
    # 與 `_dorossi_validate_dir` 走同一套正規化，否則兩者會對同一串輸入給出矛盾的
    # 答案：`"C:"` 帶引號時驗證會通過（剝掉引號後是磁碟根目錄），這裡卻因為第一個
    # 字元是 `"` 而說「不像路徑」——路徑被拒時就不會提示，靜默失敗。
    t = _dorossi_unquote_dir(text)
    if not t:
        return False
    if "/" in t or "\\" in t:
        return True
    if t.startswith("~"):
        return True
    # Windows 磁碟機字首，如 `C:`、`D:\...`。
    if len(t) >= 2 and t[0].isalpha() and t[1] == ":":
        return True
    return False


def _dorossi_parse_turn_flags(prompt: str) -> tuple:
    """解析提問「開頭」的指令 token `/effort <level>`、`/model <tier>`、
    `/session <id>`（純函式，永不 raise）。三者皆選填、順序不拘、大小寫不拘；
    只認提示最前面連續出現的指令 token——一旦遇到第一個非指令 token 就停止，所以
    出現在句中的 `/effort`（例如「請解釋 /effort 的意思」）不會被誤判、原文原樣保留。

    語意：
      * `/effort <v>`（session 級，擁有者裁決）：v ∈ DOROSSI_EFFORT_LEVELS 或
        DOROSSI_TUNE_DEFAULT（"default" ＝清除該 session 的覆寫、回到預設）；
        重複出現後者覆蓋前者。
      * `/model <m>`（session 級）：m 必須是 DOROSSI_ALL_MODEL_CHOICES 的 allowlist
        key（後端模型別名——擁有者裁決的窄範圍例外，這個功能面可露出別名）或
        "default"；回傳驗證過的 key。**allowlist 驗證是硬需求**：使用者輸入永不
        原樣進 CLI 參數，送後端時由 _dorossi_session_tuning 再查一次表取 value。
        這裡用的是**兩個後端的聯集**：純函式碰不到工作階段，不知道這一輪在哪個後端
        上。「這個後端吃不吃得下」由 dorossi_model_applies 在有工作階段的地方判，而且
        會講出來（`discord_bot._dorossi_tuning_labels`），不是安靜忽略。
      * `/session <id>`（**本輪級，不是 session 級**，擁有者需求 2026-08-27）：
        把這一輪送到指定的 session slot，**不改動 active 指標**。用途是同時對多
        個專案發問而不用來回 switch——引擎本來就支援多 session 並行（per-session
        鎖 ＋ `_dorossi_loops` registry），卡住的只是「ask 永遠打到 active」。
        值必須通過 `_dorossi_is_session_id`（`s` ＋數字）；這裡只驗**格式**，
        「該 session 存不存在」由呼叫端在狀態鎖內驗（純函式碰不到 state）。
      * 無效／缺值的指令記進 errors（(kind, raw_value)，
        kind ∈ {"effort","model","session"}），該 token 照樣被消耗、繼續往後解析
        ——呼叫端見 errors 非空就整筆拒絕並回泛用錯誤（原始值只進 stderr）。

    Returns (effort, model_key, session_id, cleaned_prompt, errors)：前三者為
    None 表示本輪未指定（effort／model 沿用 session 已存值，否則預設；session
    None ＝走 active）。cleaned_prompt 是剝掉指令後、實際要送給後端的提問（可能為
    空字串——呼叫端把「只打指令」當純設定更新處理）。呼叫端用
    _dorossi_apply_turn_tuning 把非 None 的 effort／model 寫進 session slot
    （"default" ＝清除鍵），之後每輪由 _dorossi_session_tuning 讀出生效值；
    session_id **不寫進任何 store**，它只活這一輪。"""
    effort = None
    model_tier = None
    session_id = None
    errors: list = []
    rest = (prompt or "").strip()
    while True:
        parts = rest.split(None, 1)
        if not parts:
            break
        head = parts[0].lower()
        if head not in ("/effort", "/model", "/session"):
            break
        kind = head[1:]
        tail = parts[1] if len(parts) > 1 else ""
        vparts = tail.split(None, 1)
        raw_value = vparts[0] if vparts else ""
        value = raw_value.lower()
        if kind == "effort":
            if value in DOROSSI_EFFORT_LEVELS or value == DOROSSI_TUNE_DEFAULT:
                effort = value
            else:
                errors.append((kind, raw_value))
        elif kind == "model":
            if value in DOROSSI_ALL_MODEL_CHOICES or value == DOROSSI_TUNE_DEFAULT:
                model_tier = value
            else:
                errors.append((kind, raw_value))
        else:
            if _dorossi_is_session_id(value):
                session_id = value
            else:
                errors.append((kind, raw_value))
        if not vparts:
            rest = ""  # 指令在句尾且沒帶值（已記 error）→ token 已消耗、沒東西可再解析
            break
        rest = vparts[1].strip() if len(vparts) > 1 else ""
    return effort, model_tier, session_id, rest, errors


def _dorossi_apply_turn_tuning(sess: dict, effort, model_tier) -> bool:
    """把本輪解析出的微調指令套用到 session slot（in place；session 級持久化）。
    None ＝本輪沒打該指令、不動既存值；DOROSSI_TUNE_DEFAULT ＝清除該覆寫（回到
    預設）；其餘為已驗證的合法值（effort 力度字／model 為 DOROSSI_MODEL_CHOICES
    的 allowlist key，存 key、送後端時再查表取 value——理由見表的註解）。回傳
    「是否有任何變動企圖」（有打指令就 True，供呼叫端決定要不要在確認訊息提一
    句），純函式、永不 raise。"""
    changed = False
    if effort:
        if effort == DOROSSI_TUNE_DEFAULT:
            sess.pop("tune_effort", None)
        else:
            sess["tune_effort"] = effort
        changed = True
    if model_tier:
        if model_tier == DOROSSI_TUNE_DEFAULT:
            sess.pop("tune_model", None)
        else:
            sess["tune_model"] = model_tier
        changed = True
    return changed


def _dorossi_session_tuning(sess: dict, backend: str | None = None) -> tuple:
    """讀出 session slot 目前生效的微調：回傳 (effort, model)——直接可往後端帶
    的值。effort 存的就是力度字；model 存的是 allowlist key，這裡經
    `dorossi_resolve_model` 換成實際帶給後端的值（allowlist 驗證的最後一道：只有
    查表命中才會有值）。舊版通用階層 key（fast/standard/max）讀取時無感遷移
    （不重寫 store）。未設定、或 store 裡的值已不合法（手改過／表已移除該 key）→
    該項回 None（＝維持預設、不帶旗標），永不 raise。

    **`backend` 省略時由 `dorossi_session_backend(sess)` 判**（工作階段的
    `ai_provider` 覆寫 > 模組設定）。2026-09-23 之前這支是後端盲的：它只查 claude
    那張表，所以 `/model` 設的值對 codex 完全沒作用，而且沒有任何人講出來。現在
    「這個後端吃不下這個值」回的是 None（維持後端預設），顯示端則由
    `dorossi_model_applies` 判出來並**明講**。
    """
    effort = sess.get("tune_effort")
    if effort not in DOROSSI_EFFORT_LEVELS:
        effort = None
    if backend is None:
        backend = dorossi_session_backend(sess)
    return effort, dorossi_resolve_model(backend, sess.get("tune_model"))


# --- Dorossi 自走模式（autonomous self-loop） ------------------------------------
# 擁有者授權、無人值守的多輪 agentic 工作：偵測到「自主完成、不要問我」這類意圖
# 時，Dorossi 後端會在同一個工作階段裡一輪一輪自己往前推進，直到自報完成。整個迴圈
# 期間持有 Dorossi 佇列鎖（接受的取捨，僅擁有者），`@bot abort` 可隨時中止。
#
# 完成協定：每一輪的「使用者提示詞」裡注入一個哨符指令（resume 會保留原始 system
# prompt，所以協定必須放在每輪的 user prompt）。後端完成整個任務時，在回覆最後獨立
# 一行輸出這個哨符；迴圈偵測到哨符即結束，並把哨符從對外回覆中剝掉。哨符字串屬於
# 「迴圈內部細節」，永不外洩到 Discord（串流預覽與最終回覆都會剝除）。
DOROSSI_LOOP_SENTINEL = "<<<DOROSSI-LOOP-DONE>>>"
# 共用的「實證驗證」引導：附加到下列三段每輪提示尾端（用常數串接，保持 DRY，避免
# 三段各自複製維護）。之所以要附加到「每一輪」而不是只放第一輪，是因為 resume 會保
# 留原始 system prompt，但每輪的協定／引導只活在當輪的 user prompt 裡——只放第一輪
# 的話，後面幾輪就讀不到這段守則，會退回老毛病（憑空推託、不實際驗瀏覽器改動）。
_DEFAULT_DOROSSI_LOOP_VERIFY_GUIDANCE = (
    '\n\n[驗證守則] 一、若你要做的改動牽涉瀏覽器、driver 或 Selenium 啟動路徑'
    '，不要因為「沒有瀏覽器、無法驗證」就跳過，或只憑空推論而不實際動手驗證。這個專案有一支獨立的'
    '瀏覽器驗證入口 axiomatic/verify_browser.py，預設用 tempf'
    'ile.mkdtemp() 開一個用完即丟的暫時 profile 跑煙霧驗證（headles'
    's、不碰正式登入態、不需任何憑證），直接執行它就能實證你的改動是否讓瀏覽器仍正常啟動、dri'
    'ver 仍解析得到。二、不要憑空假設限制；要主張某個限制存在，先在 repo 裡查證再下判斷'
    '。這個專案沒有 CI。driver 怎麼來，**兩個變體不一樣**：selenium 變體（'
    'webrunner_novelai.py）走 Selenium Manager（build_'
    'stealth_driver 建立 ChromeService 時並沒有指定 executa'
    'ble_path）；je 變體（webrunner_je_only.py）則是透過 je_w'
    'eb_runner，而那個套件相依 webdriver-manager>=4.0.0、並實際'
    '呼叫 ChromeDriverManager(...).install() 去取得 driv'
    'er。所以別把其中一條路的前提套到另一條，也不要把 webdriver-manager 當成'
    '這個專案沒有的東西。不要拿不存在的 CI 這類前提當理由推掉一個改動。三、需要更高的端到端把'
    '握時，verify_browser.py --full 會在隔離環境裡做「登入＋導航＋確認產'
    '圖介面可達」的完整驗證（用正式登入態的快照副本、跑完即清，不污染正式profile／不碰 t'
    'odo／不寫 pid；加 --generate 還會在隔離目錄真的產一張用完即丟的圖再清掉）'
    '。它透過跨行程鎖與正式作業序列化、偵測到正式作業在跑會自動讓位，目前接的是 selenium'
    ' 變體。讓位時印的是 `VERIFY-BROWSER: SKIP …`（exit 3），那是'
    '「這次什麼都沒驗到，晚點再來」，**不是**驗證失敗——真正的失敗才是 `FAIL`（exi'
    't 1）；看到 SKIP 不要當成自己的改動把瀏覽器弄壞了，等正式作業告一段落再驗一次即可。'
    '瀏覽器／driver 改動先用 smoke 快驗，要端到端確認再用 --full。'
)
DOROSSI_LOOP_VERIFY_GUIDANCE = load_prompt(
    "dorossi_loop_verify_guidance.md", _DEFAULT_DOROSSI_LOOP_VERIFY_GUIDANCE)
# 共用的「工具自足」引導：與 VERIFY_GUIDANCE 一樣，附加到下列三段每輪提示尾端（理
# 由相同——resume 只保留原始 system prompt，協定／引導必須每輪重注入；只放第一輪後
# 面幾輪就讀不到）。語意：把整台機器的工具鏈當成自己的，缺工具就自己取得、不要推
# 託。這是內部後端 prompt、非 Discord 對外字串，可以明講 pip／套件管理器／下載這類
# 具體手段。
_DEFAULT_DOROSSI_LOOP_TOOLING_GUIDANCE = (
    "\n\n[工具自足] 你在這台機器上有完整的 shell 與工具鏈，請把它當成你的工作環境"
    "盡量用好用滿。若任務需要某個工具、函式庫或執行檔，而機器上目前沒有，就**自己"
    "想辦法取得**之後再繼續——例如用 pip 安裝缺少的 Python 套件、用系統的套件管理器"
    "安裝命令列工具、或直接下載對應的執行檔並放到路徑上。不要因為「缺某個工具」「沒"
    "裝某個套件」就推託、跳過該步驟、或宣稱辦不到；先動手把它裝起來、取得它，再把"
    "事情做完。只有在你**真的試過各種方式取得仍然失敗**時，才如實說明你卡在哪一"
    "步、缺的是什麼、已經試過哪些方法——不要一遇到缺東西就先放棄。"
)
DOROSSI_LOOP_TOOLING_GUIDANCE = load_prompt(
    "dorossi_loop_tooling_guidance.md", _DEFAULT_DOROSSI_LOOP_TOOLING_GUIDANCE)
# B3 #5：把上面兩段「耐久守則」從『每輪 user prompt』搬到『附加系統提示』通道。實測
# --append-system-prompt 在 --resume 上「該輪生效、且不會被 baked 進 session」（故每次
# 叫用都要重帶）——放系統提示＝每輪都實際到達後端、卻不會像 user prompt 那樣累積進對話
# 歷史被後續每輪重送（避免 ~O(N²) 複利）。自走迴圈每輪（含 resume／壓縮輪）都經
# loop_system_guidance 帶上這個常數；下方 user prompt（FIRST_SUFFIX／CONTINUE／PUSHBACK）
# 只留精簡 body。守則不可直接刪——只是換成「會被保留／快取的系統提示」通道送達，後端每
# 輪仍受其約束（迴圈每輪都帶＝即使 resume 的是先前單輪建立、沒 baked 守則的 session，也
# 一樣每輪補上，故無「resume 非迴圈 session 讀不到守則」的舊 trap）。
DOROSSI_LOOP_SYSTEM_GUIDANCE = (
    DOROSSI_LOOP_VERIFY_GUIDANCE + DOROSSI_LOOP_TOOLING_GUIDANCE
)
_DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX = (
    "\n\n[自主任務] 這是一個無人值守、沒有固定終點的持續任務，請你一直做下去，不要"
    "回頭向我提問、也不要停下來等我確認；遇到需要抉擇的細節，就用合理的預設值自行"
    "決定並繼續往前推進。請主動動用你手邊所有可用的工具來完成任務——若任務需要查"
    "資料、找出可以改進的地方或更好的做法，就實際去呼叫網路搜尋工具尋找線索，不要"
    "只看眼前現有的內容就交差。做完一批之後不要停，接著主動找下一個可以改進的點"
    "繼續實作。只有在你『主動再找過一輪（包含上網搜尋），確認真的再也沒有任何值得"
    "做的事』時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL + " 當作完成"
    "訊號；只完成幾項並不算窮盡，只要還有任何事情可以做，就絕對不要輸出這個字串，"
    "繼續推進就好。"
)  # 耐久守則改走系統提示（DOROSSI_LOOP_SYSTEM_GUIDANCE），不再附在 user prompt
# 檔案裡哨符處寫 {sentinel}，載入時換回 DOROSSI_LOOP_SENTINEL（哨符單一來源在程式碼）。
DOROSSI_LOOP_FIRST_SUFFIX = load_prompt(
    "dorossi_loop_first_suffix.md", _DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
_DEFAULT_DOROSSI_LOOP_CONTINUE_PROMPT = (
    "繼續推進這個沒有固定終點的持續任務，不要停下來問我問題、也不要等我確認；遇到"
    "抉擇就用合理的預設值自行決定。請主動動用所有可用工具，必要時實際呼叫網路搜尋"
    "工具找出新的可改進點或更好的做法，不要只看眼前內容就交差。做完一批就接著找下"
    "一個可以改進的地方繼續實作。只有在你主動再找過一輪（含上網搜尋）、確認真的徹底"
    "沒有任何值得做的事時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL +
    "；只完成幾項不算窮盡，只要還有事情可以做就不要輸出，繼續做下去。"
)  # 耐久守則改走系統提示（DOROSSI_LOOP_SYSTEM_GUIDANCE），不再附在 user prompt
DOROSSI_LOOP_CONTINUE_PROMPT = load_prompt(
    "dorossi_loop_continue.md", _DEFAULT_DOROSSI_LOOP_CONTINUE_PROMPT,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
_DEFAULT_DOROSSI_LOOP_PUSHBACK_PROMPT = (
    "你剛才表示告一段落，但這是一個持續任務，還沒到可以停下來的時候。請你再主動找"
    "一輪可以改進的地方（必要時上網搜尋新的點子或更好的做法），並繼續實作下去；遇到"
    "抉擇就用合理的預設值自行決定，不要回頭問我。只有在你真的徹底找過、確認再也沒有"
    "任何值得做的事時，才在回覆的最後『獨立一行』輸出 " + DOROSSI_LOOP_SENTINEL +
    "；只要還有任何事情可以做，就不要輸出，繼續推進。"
)  # 耐久守則改走系統提示（DOROSSI_LOOP_SYSTEM_GUIDANCE），不再附在 user prompt
DOROSSI_LOOP_PUSHBACK_PROMPT = load_prompt(
    "dorossi_loop_pushback.md", _DEFAULT_DOROSSI_LOOP_PUSHBACK_PROMPT,
    replacements={"sentinel": DOROSSI_LOOP_SENTINEL})
# 「週期性壓縮」維護輪送出的提示：用後端自身的 `/compact` slash command（已實測 headless
# `claude -p` 經 STDIN 可靠生效、保留 session id），並以 focus 參數明確要求保留任務續跑
# 所需的脈絡（壓縮有損——實測會摘要掉細節，故必須點名要保住任務／待辦／決策）。這一輪
# 只做壓縮、不產出任務進度（result 多半為空），由迴圈當「維護輪」處理：不計入 idle、不貼
# 輸出、壓完重置計數後續跑。此為內部後端提示、永不對外送出。
_DEFAULT_DOROSSI_LOOP_COMPACT_PROMPT = (
    "/compact 請保留以下脈絡以便無縫接續這個持續任務：整體任務目標與限制、所有尚未"
    "完成的待辦與接下來的步驟、已完成的重點、關鍵決策與踩過的雷、目前正在進行的工作"
    "狀態；可以省略無關的閒聊與冗長的中間輸出，但上述任務脈絡務必完整摘要、不要遺漏。"
)
DOROSSI_LOOP_COMPACT_PROMPT = load_prompt(
    "dorossi_loop_compact.md", _DEFAULT_DOROSSI_LOOP_COMPACT_PROMPT)
# 連續這麼多輪「沒有進展」（後端自報完成，或該輪根本沒產出）才停止整個自走迴圈。
# 用來提高過早收工的門檻：後端做幾項就吐哨符時，會先被推回再找一輪，連續多輪都沒
# 進展才真正放手。純模組常數即可（不塞進 bot_config，以免缺鍵破壞載入器）。
DOROSSI_LOOP_EXHAUSTION_ROUNDS = 3

# 後端自判（混合觸發第二條路徑）：閘門開（擁有者＋claude_code＋full）但提問沒命中自走
# 片語時，在 turn-1 的提示尾端注入「自我評估」指示，讓後端自己判斷這是不是需要連續多輪
# 無人值守推進到完成的較大任務；若是，就在 turn-1 回覆最後『獨立一行』輸出「開場哨符」。
# bot 偵測到開場哨符就把 turn-1 當第一輪、轉進自走迴圈從第二輪續跑。開場哨符與完成哨符
# DOROSSI_LOOP_SENTINEL 是「不同字串、職責不同」：開場＝要不要進迴圈；完成＝迴圈要不要
# 停。兩者皆屬迴圈內部訊號，永不外洩到 Discord（串流預覽與最終回覆都會剝除）。
DOROSSI_LOOP_OPEN_SENTINEL = "<<<DOROSSI-LOOP-OPEN>>>"
# 自判指示措辭刻意保守：只有「單則答不完、需要連續多輪無人值守推進到完成」的較大任務才
# 吐開場哨符；一般問答／查詢／單則可答完的請求一律不要吐，拿不準時也不要吐——避免一般
# 問題被後端誤判成自走。
_DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX = (
    "\n\n[自我評估] 先照常完整回答、或著手處理上面的請求。處理完之後，請你自己評估："
    "這個請求是不是一個『單則回覆答不完、需要連續多輪、無人值守地自主推進直到完成』的"
    "較大任務？只有在你確定屬於這種任務時，才在整段回覆的最後『獨立一行』輸出 "
    + DOROSSI_LOOP_OPEN_SENTINEL + " 這個字串，表示你要繼續多輪自主把它完成；若這只是"
    "一般問答、查詢、或單則就能回覆完的請求，就『絕對不要』輸出這個字串，正常回覆即可。"
    "拿不準時一律不要輸出。這個字串純屬內部訊號，不要對它多做任何說明或解釋。"
)
# 檔案裡開場哨符處寫 {open_sentinel}，載入時換回 DOROSSI_LOOP_OPEN_SENTINEL。
DOROSSI_LOOP_SELFJUDGE_SUFFIX = load_prompt(
    "dorossi_loop_selfjudge_suffix.md", _DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX,
    replacements={"open_sentinel": DOROSSI_LOOP_OPEN_SENTINEL})
# 後端自判要續跑時、轉進自走迴圈的 ack。措辭刻意與「明確下令進自走」的 ack 不同，讓擁
# 有者一眼看出是後端自己判斷這個任務較大、需要持續推進（而非自己明確下了自走指令）。
DOROSSI_SELF_JUDGE_ACK = (
    "🔁 這個任務較大，我會持續推進直到完成或你喊停（`@bot abort`）。"
)

# 觸發自走模式的「意圖片語」。比對策略刻意精準：只有同時通過（擁有者 ＋ full 工具
# 模式 ＋ 命中下列其一）才會進入自走模式，避免一般問題誤觸。CJK 直接子字串比對，
# 英文走 lower-case 比對。
# 清單也涵蓋「本專案自己對這個模式的稱呼」：擁有者很自然會用專案術語下令，而不是只
# 用「不要問我／做到完成」這類泛用講法。其中「自走」是本專案對此模式的專名，擁有者
# 講「自走」幾乎必然就是要這個模式，誤觸風險低，直接當裸子字串收。「循環」相關則一律
# 只收複合片語（循環模式／一直循環／自走循環／循環下去…），刻意不收裸詞「循環」——
# 否則 full 模式下問「這個 for 迴圈有 bug」「這段循環怎麼寫」之類會誤觸。
# 「循環模式／迴圈模式」是擁有者實際用來指稱本模式的講法（與「自走模式」同義），必須
# 在清單內；「模式」兩字把它與泛指程式迴圈的裸詞區隔開，誤觸風險等同「自走」。反過來，
# 「進入循環／開始循環／自動循環」這類**可以在描述程式行為時出現**的講法刻意不收
# （「程式進入循環後就卡住」會誤觸，誤觸＝白跑好幾輪、燒 token），要下令請用「循環
# 模式」或既有片語。
_DOROSSI_LOOP_INTENT_SUBSTRINGS = (
    "不要問我", "不用問我", "別問我", "不要再問我", "別再問我",
    "不要回頭問", "不要問問題", "別問問題",
    "做到完成", "做到完為止", "做完為止", "做到好為止",
    # 刻意不收「自動完成」：那是編輯器 autocomplete 的標準譯名，「VS Code 的自動完成怎麼
    # 關掉」會直接起一個無人值守的迴圈（2026-09-22 拿掉）。
    "自己做完", "自己完成", "自行完成", "自主完成",
    "持續做", "持續推進", "不要停下來", "無人值守",
    # 本專案自有術語：「自走」是專名（裸詞即收）；「循環」一律收複合片語。
    "自走",
    "循環模式", "迴圈模式",
    # 「一直循環」留著：那是擁有者實際下令用的講法（2026-07-26 回報「明確說要一直循環」
    # 卻沒進迴圈），代價是「程式一直循環停不下來」這種問句也會命中。「不斷循環」「反覆循環」
    # 「一直迴圈」沒有這個理由，又正是描述程式卡住的講法（在台灣「迴圈」就是程式的 loop），
    # 2026-09-22 拿掉，與「進入循環」不收同一條規則。
    "一直循環", "自走循環", "持續循環", "循環下去",
    "持續迴圈", "迴圈下去",
    # 英文刻意不收描述程式行為的講法（2026-09-22 拿掉 `loop until` / `loop forever` /
    # `keep looping` / 裸的 `without asking`）：「how do I loop until the list is
    # empty」「why does it keep looping」「install without asking for confirmation」
    # 都是一般的程式問題，跟中文不收「進入循環」同一條理由。
    "don't ask me", "do not ask me", "without asking me",
    "keep going until", "until it is done", "until it's done", "until done",
    "do it autonomously", "work autonomously", "autonomously until",
    "loop mode", "autonomous mode",
)
# 含觸發詞、意思卻完全無關的複合詞：比對前先整個遮掉。「自走砲」是遊戲與軍事用語（擁有者
# 玩的正是這類遊戲）、「自走式」是機具的形容詞、「持續整合」是 CI、「持續時間」「持續性」
# 是一般名詞。遮掉的是那個詞本身，同一句裡另外寫的「自走模式」照樣命中。
_DOROSSI_LOOP_INTENT_MASKS = (
    "自走砲", "自走炮", "自走式",
    "持續整合", "持續時間", "持續性",
)
# 跨字的「一直做到…完成」類片語：(開頭, 結尾, 中間必須含其一)。每一筆都套同一組規則
# （`_dorossi_loop_pair_hit`），新增的配對自動適用，不要另寫一次性的 if：
#   1. 有順序——開頭在結尾前面；
#   2. 中間不超過 `_DOROSSI_LOOP_PAIR_MAX_GAP` 個字、不跨句；
#   3. 中間不含 `_DOROSSI_LOOP_PAIR_ENDPOINTS`——「先做到這裡為止就好」「做到今天為止」
#      講的是停在哪裡，跟「做到完成為止」剛好相反；「才」是敘述語氣（「一直做到半夜
#      才完成」「持續多久才完成」）；
#   4. 第三欄非空時，中間必須含其中之一——「持續…完成」要有「到」（直到完成／修到完成），
#      否則「持續整合的設定完成了嗎」這種問句也算。
# 2026-09-22 以前是「兩個字串出現在任何位置、任何順序」就算，實測「你目前為止做到哪裡了？」
# 「先做到這裡為止就好」都會起一個無人值守的迴圈；而整個測試套件裡這條分支一次都沒有
# 回過 True（有配對的測試句都先被單一片語命中），所以沒有人發現。
_DOROSSI_LOOP_INTENT_PAIRS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("一直做到", "完成", ()),
    ("做到", "為止", ()),
    ("持續", "完成", ("到",)),
)
_DOROSSI_LOOP_PAIR_MAX_GAP = 24
_DOROSSI_LOOP_PAIR_ENDPOINTS = (
    "這裡", "這邊", "這兒", "這樣", "這步", "這一步", "這個階段", "此",
    "那裡", "那邊", "哪", "目前", "現在", "至今", "今天", "一半", "才",
)
_DOROSSI_LOOP_PAIR_SENTENCE_BREAKS = frozenset("。！？!?；;\n")

# 本模式的**名字**出現在問句裡，多半是在問這個功能，不是在下令（2026-09-22）。實例：
# 「現在是否已經支援 Discord 上的平行多個執行 /dorossi ask 或自走模式」起了一個自走迴圈——
# 名字「自走」是裸子字串，而那一句是問句。所以名字要算數，得是「不是問句」，或是問句但名字
# 前面緊接著一個啟動動詞、而且不是在問怎麼做（「可以進入自走模式幫我補完嗎」算、「要怎麼進入
# 自走模式？」不算）。一般的下令片語（不要問我、做到完成…）不受這條影響。
_DOROSSI_LOOP_MODE_NAMES = (
    "自走", "自走循環", "循環模式", "迴圈模式", "loop mode", "autonomous mode",
)
_DOROSSI_QUESTION_MARKERS = (
    "？", "?", "嗎", "呢", "是否", "有沒有", "能不能", "可不可以", "會不會", "是不是",
    "支不支援",
)
_DOROSSI_HOW_MARKERS = (
    "怎麼", "如何", "為什麼", "為何", "什麼", "哪", "how ", "what ", "why ", "which ",
)
_DOROSSI_LOOP_INVOKE_VERBS = (
    "進入", "開啟", "啟動", "打開", "開", "用", "使用", "切到", "切換到", "改用", "改成", "以",
    "跑", "進", "enter ", "use ", "start ", "switch to ", "run in ", "turn on ", "go into ",
)


_DOROSSI_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def _dorossi_loop_mode_name_counts(text: str, name: str) -> bool:
    """模式名字 `name` 在 `text`（已小寫）裡算不算下令。見
    `_DOROSSI_LOOP_MODE_NAMES` 上面的說明。

    **逐句判斷**：問號與「為什麼」只管它自己那一句——「我不知道為什麼測試會紅？進入自走
    模式把它修好」的下令在第二句，整段一起看的話會被第一句的問號與「為什麼」吃掉。"""
    for sentence in _DOROSSI_SENTENCE_SPLIT_RE.split(text):
        if name not in sentence:
            continue
        if not any(marker in sentence for marker in _DOROSSI_QUESTION_MARKERS):
            return True
        if any(marker in sentence for marker in _DOROSSI_HOW_MARKERS):
            continue
        start = sentence.find(name)
        while start != -1:
            before = sentence[max(0, start - 12):start]
            if any(before.endswith(verb) for verb in _DOROSSI_LOOP_INVOKE_VERBS):
                return True
            start = sentence.find(name, start + 1)
    return False


def _dorossi_loop_pair_hit(text: str, head: str, tail: str,
                           gap_needs: tuple[str, ...] = ()) -> bool:
    """`text` 裡有沒有一段「`head` …（中間）… `tail`」符合配對規則（見
    `_DOROSSI_LOOP_INTENT_PAIRS` 上面的四條）。每一個 `head` 出現的位置都試，
    所以前面一段不算數的，不會擋掉後面真正的那一段。"""
    start = text.find(head)
    while start != -1:
        gap_from = start + len(head)
        end = text.find(tail, gap_from)
        if end != -1:
            gap = text[gap_from:end]
            if (len(gap) <= _DOROSSI_LOOP_PAIR_MAX_GAP
                    and not any(ch in _DOROSSI_LOOP_PAIR_SENTENCE_BREAKS
                                for ch in gap)
                    and not any(w in gap for w in _DOROSSI_LOOP_PAIR_ENDPOINTS)
                    and (not gap_needs or any(w in gap for w in gap_needs))):
                return True
        start = text.find(head, start + 1)
    return False


def _dorossi_matches_loop_intent(prompt: str) -> bool:
    """提問是否帶有「自主完成、不要問我」的自走意圖。比對刻意保守：誤觸的代價是一個
    無人值守的迴圈，漏掉的代價是擁有者得換個明確的說法。

    ⚠️ 不要假設「漏掉了還有後端自判會再問一次」：那條路徑由 `dorossi_self_judge_enabled`
    控制，本機 `bot_config.json` 把它關掉了（2026-09-22 查到），所以在這台主機上這支函式
    是進入自走的**唯一**入口——收緊它的時候，真正的下令講法一句都不能漏。"""
    if not prompt:
        return False
    for mask in _DOROSSI_LOOP_INTENT_MASKS:
        prompt = prompt.replace(mask, "\x00")
    low = prompt.lower()
    for sub in _DOROSSI_LOOP_INTENT_SUBSTRINGS:
        if sub in prompt or sub in low:
            if (sub in _DOROSSI_LOOP_MODE_NAMES
                    and not _dorossi_loop_mode_name_counts(low, sub)):
                continue
            return True
    return any(_dorossi_loop_pair_hit(prompt, head, tail, needs)
               for head, tail, needs in _DOROSSI_LOOP_INTENT_PAIRS)


def _dorossi_remove_all_sentinels(text: str, markers: tuple[str, ...]) -> str:
    """把 `markers` 從 `text` 裡移除到**一個都不剩**，而不是只掃一遍。

    `str.replace` 掃一遍就結束，但「移除」本身會把左右兩邊接起來，所以夾心寫法
    （`<<<DOROSSI-LOOP-` ＋ 一個完整哨符 ＋ `DONE>>>`）在移掉內層之後會**重新拼出**
    一個完整的哨符，單次 replace 的結果因此可能仍然含有哨符。剝除這幾支的整個存在
    理由就是「送出去之前一個都不能剩」，所以掃到不再變動為止。

    兩個哨符共用 `<<<DOROSSI-LOOP-` 這段長前綴，移掉其中一個也可能拼出另一個，
    所以是「每一輪把所有 marker 都掃一次，整輪沒變動才收手」，不是逐個 marker 各
    自收斂。每一次有效的替換都讓字串變短，所以一定會停。
    """
    while True:
        before = text
        for marker in markers:
            text = text.replace(marker, "")
        if text == before:
            return text


def _dorossi_strip_loop_sentinel(answer: str) -> tuple[str, bool]:
    """回傳 (cleaned, done)。done 為 True 表示這一輪輸出含完成哨符；cleaned 已把所有
    哨符出現處移除並修整前後空白。用於「最終的每輪答案」以判定是否結束迴圈，並確保
    哨符不會出現在對外回覆。"""
    if not answer or DOROSSI_LOOP_SENTINEL not in answer:
        return answer, False
    return _dorossi_remove_all_sentinels(
        answer, (DOROSSI_LOOP_SENTINEL,)).strip(), True


def _dorossi_strip_open_sentinel(answer: str) -> tuple[str, bool]:
    """回傳 (cleaned, opened)。opened 為 True 表示 turn-1 輸出含「開場哨符」（後端自判
    這是需要多輪自主完成的較大任務）；cleaned 已把所有哨符出現處移除並修整前後空白。
    用於後端自判路徑的 turn-1 答案，確保開場哨符絕不出現在對外回覆。"""
    if not answer or DOROSSI_LOOP_OPEN_SENTINEL not in answer:
        return answer, False
    return _dorossi_remove_all_sentinels(
        answer, (DOROSSI_LOOP_OPEN_SENTINEL,)).strip(), True


# 串流預覽要剝除的所有內部哨符（完成 ＋ 開場）。兩者皆屬迴圈／自判內部訊號，絕不可
# 在 live 預覽裡一閃而過。
_DOROSSI_STREAM_SENTINELS = (DOROSSI_LOOP_SENTINEL, DOROSSI_LOOP_OPEN_SENTINEL)


def _dorossi_redact_sentinel_stream(text: str) -> str:
    """串流預覽用：移除已完整出現的哨符（完成／開場皆含），並把「結尾的半截哨符
    （前綴）」也藏起來，避免半串流出來的哨符在 live 預覽裡一閃而過。只動結尾的前綴，
    不影響正文。兩個哨符共用長前綴，但結尾只可能是其中一個的前綴；逐一檢查、命中即
    回，故不會互相干擾。"""
    if not text:
        return text
    text = _dorossi_remove_all_sentinels(text, _DOROSSI_STREAM_SENTINELS)
    for sentinel in _DOROSSI_STREAM_SENTINELS:
        for i in range(len(sentinel) - 1, 0, -1):
            if text.endswith(sentinel[:i]):
                return text[:-i]
    return text


_dorossi_client = None  # lazily-constructed AsyncAnthropic singleton

# api 後端的單次請求逾時與 SDK 自帶重試次數。**寫出來是刻意的，不是複製預設值。**
#
# 這條路沒有串流，所以 claude_code 那套兩層 watchdog（閒置層 ＋ 硬性牆鐘）在這裡
# 一層都用不上：`messages.create()` 就是一次 await，唯一的界限就是 SDK 的逾時。
# 而 `anthropic` 在 `requirements.txt` 裡沒有釘版本（fresh clone 拿最新是刻意的），
# 所以「界限」等於「這一版 SDK 的預設值」——那不是本專案做的決定，而且會在升版時
# 無聲改變。無人值守的自走迴圈最不該有的就是一個會自己漂移的時間上限。
#
# 這兩個值**與 anthropic 1.3.0／1.4.0 的預設完全相同**（read timeout 600s、重試 2 次），
# 所以明寫它們不改變行為，只是把它從「繼承來的」變成「選定的」。
#
# **但「最壞 600 × (1 + 2) ＝ 30 分鐘」這句話，光靠這兩個值是不成立的（2026-09-19）。**
# 兩次重試**之間**還有 SDK 自己的睡眠，而 anthropic 1.6.0 改了它：
# `_calculate_retry_timeout` 原本只在 `0 < retry_after <= 60` 時照伺服器的
# `Retry-After` 睡，否則退回自己的指數退避（最多 8 秒）；1.6.0 起改成
# `min(retry_after, 4_294_967.0)`，也就是**伺服器說睡多久就睡多久**。本機假伺服器
# 實測 429 ＋ `retry-after: 3600`：1.4.0 是 0.4／0.8 秒各重試一次、1.3 秒丟
# `RateLimitError`；1.7.0 是每次重試前睡 3600 秒，一輪卡約 2 小時。fresh clone 今天
# 拿到的就是 1.7.0。後果有兩層：這一輪握著工作階段鎖卡兩小時；而 bot 自己的用量上限
# 處理（`reset_at = now + retry_after`）要等 SDK 睡完才拿得到例外，算出來的重設時刻
# 還**晚了已經睡掉的那段**。
#
# 所以現在有兩層，都是本專案自己的東西，不隨 SDK 版本漂：
#   1. `_dorossi_api_clamp_retry_after`（http client 的 response hook）：伺服器要求的
#      等待超過 `_DOROSSI_API_SDK_SLEEP_CAP_SEC`（60 秒，正是 1.6.0 之前 SDK 自己的
#      上限）時，在回應上補 `x-should-retry: false`，SDK 就不自己重試、立刻丟例外，
#      `retry-after` 標頭原樣留在例外上給 `_dorossi_api_retry_after_sec` 讀。短的等待
#      與沒帶等待的 5xx／529 照舊由 SDK 重試。
#   2. `_dorossi_api_call_ceiling_sec()`：`_dorossi_via_api` 用 `asyncio.timeout` 把
#      整次 `messages.create()` 框起來。上面那句 30 分鐘本來就不精確——600 是 read
#      timeout（位元組之間的間隔），不是一次請求的總長——所以「最壞多久」必須由本專案
#      自己強制，而不是從 SDK 的參數推論出來。
# 要不要收緊這些數字是擁有者的調校決定（比 claude_code 純聊天模式的硬上限
# `dorossi_cc_hard_limit_off_sec`＝900s 寬一倍），寫在這裡是為了讓那個決定看得見。
DOROSSI_API_TIMEOUT_SEC = 600.0
DOROSSI_API_MAX_RETRIES = 2
# SDK 內建重試**每一次**最多准睡幾秒（見上面第 1 層）。60 不是新挑的數字：它是
# anthropic 1.6.0 之前 SDK 自己寫死的上限，所以在 1.4.0 上這一層的效果只是「超過
# 60 秒就不重試」，不會讓任何原本會發生的短等待消失。
_DOROSSI_API_SDK_SLEEP_CAP_SEC = 60.0
# 外框的額外餘裕：連線建立、排程抖動。刻意小——外框的用途是「最壞情況有界」，
# 不是「剛好容得下最慢的正常回合」。
_DOROSSI_API_CEILING_SLACK_SEC = 30.0


def _dorossi_api_call_ceiling_sec() -> float:
    """一次 `messages.create()`（含 SDK 內建重試）最多准跑幾秒。

    ＝ 每次請求的逾時 × 請求次數 ＋ 每次重試前最多睡 `_DOROSSI_API_SDK_SLEEP_CAP_SEC`
    ＋ 餘裕。預設 600 × 3 ＋ 60 × 2 ＋ 30 ＝ 1950 秒。**在呼叫時才讀模組常數**，不寫成
    預設引數（預設引數在 `def` 當下就綁死，之後改常數不會生效）。"""
    retries = max(0, int(DOROSSI_API_MAX_RETRIES))
    return (float(DOROSSI_API_TIMEOUT_SEC) * (1 + retries)
            + retries * _DOROSSI_API_SDK_SLEEP_CAP_SEC
            + _DOROSSI_API_CEILING_SLACK_SEC)


def _dorossi_sdk_retry_wait_sec(headers, *, now: float | None = None) -> float | None:
    """SDK 內建重試遇到這組回應標頭時，**會**打算睡幾秒。回 None ＝ 標頭沒有給等待。

    **逐步照抄 SDK 自己的 `_parse_retry_after_header`**（1.4.0 與 1.7.0 逐字相同），
    包括它的優先順序與怪癖，因為這支要回答的問題是「SDK 會怎麼做」，不是「伺服器的
    意思是什麼」：
      1. `retry-after-ms`（非標準、毫秒）轉得成 float 就用它——**即使是 nan 或負數**，
         SDK 也不會再往下看；
      2. 否則 `retry-after` 轉得成 float 就當秒數（SDK 容許小數）；
      3. 否則把 `retry-after` 當 HTTP 日期（`email.utils.parsedate_tz` ＋ `mktime_tz`，
         沒有時區時照本機時間解讀——SDK 就是這樣做的），回「那個時刻減掉現在」，可能
         是負數。
    回傳值可能是 nan／inf／負數，**呼叫端自己判斷**（`_dorossi_api_clamp_retry_after`
    只問「> 60 嗎」，nan 自然不過、inf 自然過，正好對應 SDK 的 `retry_after > 0`）。

    刻意**不**拿來取代 `_dorossi_api_retry_after_sec`：那一支回答的是「值不值得拿來排
    等待」，只收有限的正秒數，HTTP 日期一律不收（有測試釘住）。兩支問的是兩個問題。
    SDK 那份私有實作若哪天改了，`test_dorossi_api_retry` 的對照測試會拿同一份語料去問
    兩邊。永不 raise——這支跑在 http client 的 hook 裡，炸了會把一個正常的回應換成例外。
    """
    try:
        if headers is None:
            return None
        try:
            return float(headers.get("retry-after-ms", None)) / 1000
        except (TypeError, ValueError):
            pass
        raw = headers.get("retry-after")
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
        parsed = email.utils.parsedate_tz(raw)
        if parsed is None:
            return None
        when = email.utils.mktime_tz(parsed)
        return float(when - (time.time() if now is None else now))
    except Exception:  # pylint: disable=broad-except
        return None


async def _dorossi_api_clamp_retry_after(response) -> None:
    """http client 的 response event hook：伺服器要求的等待超過
    `_DOROSSI_API_SDK_SLEEP_CAP_SEC` 時，叫 SDK 不要自己重試。

    做法是在回應上補 `x-should-retry: false`——SDK 的 `_should_retry` 第一件事就是看
    這個標頭（1.4.0 與 1.7.0 都是），看到 `"false"` 就不重試、直接把錯誤往上丟。
    `retry-after` 本身**不動**，所以 `_dorossi_via_api` 照樣從例外上讀得到它，
    `_DorossiUsageLimitError.reset_at` 也就是「現在 ＋ 伺服器要的秒數」，不會再晚上
    SDK 已經睡掉的那段。

    **不限於 429**：1.6.0 起任何會被重試的狀態碼（408／409／429／5xx）都會照
    `Retry-After` 睡滿，所以判準是「SDK 會不會睡超過 60 秒」，不是狀態碼。沒帶等待
    （或等待 ≤ 60 秒）的 5xx／529 照舊由 SDK 用自己的退避重試。2xx 不碰。

    必須是 async（`AsyncClient` 會 await 每一個 hook）。**永不 raise**：hook 丟的例外
    會把一個本來可以正常處理的回應換成一個看不懂的新例外。出事只記型別名到 stderr
    ——這行會進 `discord_bot.log`，而 `/log tail` 是那個檔的對外出口。"""
    try:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return
        wait = _dorossi_sdk_retry_wait_sec(response.headers)
        if wait is None or not wait > _DOROSSI_API_SDK_SLEEP_CAP_SEC:
            return
        response.headers["x-should-retry"] = "false"
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] retry-after clamp skipped ({type(exc).__name__})",
              file=sys.stderr)


def _dorossi_api_http_client():
    """帶著 `_dorossi_api_clamp_retry_after` 的 http client；SDK 不提供工廠時回 None。

    用 SDK 自己的 `DefaultAsyncHttpxClient`（公開 API），所以連線上限、預設逾時、
    跟隨轉址與不帶 `http_client` 時完全相同（2026-09-19 在 1.4.0／1.7.0 實測：連線上限
    都是 1000、逾時 600）——唯一的差別就是那個 hook。"""
    factory = getattr(anthropic, "DefaultAsyncHttpxClient", None) if anthropic else None
    if factory is None:
        return None
    return factory(event_hooks={"response": [_dorossi_api_clamp_retry_after]})


def _get_dorossi_client():
    """Lazily build + cache the Anthropic async client. Returns None if the SDK
    is missing or no credentials can be resolved (construction is offline, so a
    failure here means an auth/config problem, not a network one).

    逾時與重試次數一律明寫（見 `DOROSSI_API_TIMEOUT_SEC`）：這條路沒有串流，
    SDK 的逾時就是唯一的時間界限，不能讓它跟著相依套件的預設值漂。
    http client 帶著 retry-after 夾子（同一段說明的第 1 層）；夾子裝不上時照樣建
    client、在 stderr 講一句——外框（第 2 層）仍然守著最壞時間，不為了一個準確度
    的改善把整個後端關掉。"""
    global _dorossi_client
    if _dorossi_client is not None:
        return _dorossi_client
    if AsyncAnthropic is None:
        return None
    kwargs = {"timeout": DOROSSI_API_TIMEOUT_SEC,
              "max_retries": DOROSSI_API_MAX_RETRIES}
    try:
        http_client = _dorossi_api_http_client()
    except Exception as exc:  # pylint: disable=broad-except
        http_client = None
        print(f"[dorossi] retry-after clamp could not be installed "
              f"({type(exc).__name__}); long server-requested waits are still "
              f"bounded by the {_dorossi_api_call_ceiling_sec():.0f}s call ceiling",
              file=sys.stderr)
    if http_client is not None:
        kwargs["http_client"] = http_client
    try:
        _dorossi_client = AsyncAnthropic(**kwargs)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] client init failed: {exc!r}", file=sys.stderr)
        return None
    return _dorossi_client


class _DorossiResumeError(RuntimeError):
    """Raised when `claude -p --resume <id>` fails because the stored session is
    gone (expired / cleaned). The caller retries once with a fresh session."""


class _DorossiTransientError(RuntimeError):
    """後端回了一個**伺服器側、通常會自己好**的錯誤（529 Overloaded、502/503/504
    之類），不是工作階段過舊、也不是方案用量上限。

    2026-09-03 補。在這之前這種錯誤會一路變成 `RuntimeError` 落進自走迴圈的泛用
    `except`，於是**整個無人值守的迴圈就地停掉**——而錯誤訊息自己寫著「usually
    temporary — try again in a moment」。實測 21:31 與 21:36 連兩次 529（中間那次
    「重試」是把工作階段丟掉重開，對伺服器過載完全沒有幫助），第二次就收工了。

    `status` 給程式判斷（重試要等多久），`reason` 是給 log 的短字串——**不要**把它
    直接送到對話平台，那是未經控制的外部文字（Layer 1）。
    """

    def __init__(self, reason: str, *, status: int | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.session_id = session_id


# 會自己好的 HTTP 狀態。429 **不在**這裡：那是用量／速率上限，由
# `_dorossi_cc_usage_limit` 走它自己的等待路徑（有 reset 時間可以等）。
DOROSSI_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 529})

# 文字標記。狀態碼有時候不會被帶進 result 事件，只留下英文訊息。
_TRANSIENT_TEXT_MARKERS = (
    "overloaded",            # 529 Overloaded / overloaded_error
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporarily unavailable",
)


class _DorossiOfflineError(RuntimeError):
    """後端**連不上它的伺服器**：DNS 解析失敗、連線被拒／被重設、網路不可達。

    2026-09-22 補。那天 17:32–18:37 本機 DNS 整段失效，後端 CLI 回的是
    `API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)`，
    而這一類在此之前沒有任何分類：一路落到判定的最後一條，被當成「工作階段過舊」
    ——**丟掉脈絡、開新工作階段重試**（新的那次當然一樣連不上），然後自走迴圈的
    泛用重試用完三次就停，六個正在跑的任務全部停在原地。

    處理方式跟用量上限、暫時性故障同一個原則：**等，而且用同一個工作階段重跑**。
    差別在「等到什麼時候」：這裡等的是網路回來（呼叫端輪詢連線），不是一段猜的秒數，
    也**沒有次數上限**——擁有者裁決不得有回合／花費類上限，等網路只能被 abort 結束。

    `backend` 給呼叫端決定要探測哪一個主機；`reason` 是給 log 的短字串——**不要**
    送到對話平台（Layer 1）。`session_id` 是這次叫用**傳進去**的那一個（續接用）。
    """

    def __init__(self, reason: str, *, backend: str | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(reason)
        self.backend = backend
        self.session_id = session_id


# 「連不上伺服器」的文字標記（小寫比對）。來源分兩種：CLI 的 result 文字（實測
# 2026-09-22：`API Error: Can't reach the API server — check your internet or DNS
# (ENOTFOUND)`），以及 CLI／SDK 寫到 stderr 或錯誤事件的底層錯誤碼。
_OFFLINE_TEXT_MARKERS = (
    "can't reach the api server",
    "cannot reach the api server",
    "unable to connect to api",
    "enotfound",
    "eai_again",
    "econnreset",
    "econnrefused",
    "etimedout",
    "enetunreach",
    "ehostunreach",
    "getaddrinfo",
    "network is unreachable",
    "failed to lookup address",
    "dns error",
    "error sending request for url",
)
# result 文字那一條的長度上限：CLI 的通知是一行模板，會談到「連線錯誤」的答案是散文。
# 與用量上限、未登入那兩條同一個理由（一篇剛好討論 ECONNRESET 的長答案不能被判成離線）。
_DOROSSI_OFFLINE_NOTICE_MAX_CHARS = 300


def _dorossi_offline_marker_in(text) -> bool:
    """`text` 裡有沒有「連不上伺服器」的字樣。永不 raise。"""
    try:
        low = str(text or "").lower()
    except Exception:  # pylint: disable=broad-except
        return False
    return any(marker in low for marker in _OFFLINE_TEXT_MARKERS)


def _dorossi_cc_offline(result_ev, err: str = "", session_id: str | None = None
                        ) -> "_DorossiOfflineError | None":
    """rc != 0 的這一輪，是不是「連不上伺服器」？是就回填好的例外，否則 None。

    證據兩處：`result` 事件（`is_error` 為真、文字短得像一則通知）與 stderr。成功完成
    的 result 一律不是（`_claude_result_succeeded`）。純函式、永不 raise。"""
    try:
        if _claude_result_succeeded(result_ev):
            return None
        ev = result_ev if isinstance(result_ev, dict) else {}
        text = ev.get("result")
        hit = (ev.get("is_error") and isinstance(text, str)
               and len(text.strip()) <= _DOROSSI_OFFLINE_NOTICE_MAX_CHARS
               and _dorossi_offline_marker_in(text))
        if not hit and _dorossi_offline_marker_in(err):
            hit = True
        if not hit:
            return None
        return _DorossiOfflineError(
            (str(text or "") or str(err or ""))[:400] or "offline",
            backend="claude_code", session_id=session_id)
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_codex_offline(text, session_id: str | None = None
                           ) -> "_DorossiOfflineError | None":
    """codex 的 stderr／失敗事件像不像「連不上伺服器」。只從 rc != 0 那條被呼叫，
    餵進來的文字不含答案（見 `_dorossi_codex_usage_limit` 的說明）。"""
    if not _dorossi_offline_marker_in(text):
        return None
    return _DorossiOfflineError(str(text)[:400], backend="codex",
                                session_id=session_id or None)


def _dorossi_api_is_offline(exc) -> bool:
    """SDK 例外是不是連線層的失敗（`APIConnectionError`）。永不 raise。

    **逾時（`APITimeoutError`，它是 `APIConnectionError` 的子類）不算**：那是連上了、
    對方一直沒回，不是網路斷了——等網路回來不會讓它變好，照舊走既有的例外路徑
    （`test_dorossi_api_retry` 釘著它原樣往上拋）。有狀態碼的（`APIStatusError` 家族）
    也一律不是——那是伺服器回了話，走用量上限／暫時性故障／其他的既有分類。"""
    try:
        timeout_cls = getattr(anthropic, "APITimeoutError", None) if anthropic else None
        if timeout_cls is not None and isinstance(exc, timeout_cls):
            return False
        conn = getattr(anthropic, "APIConnectionError", None) if anthropic else None
        if conn is not None and isinstance(exc, conn):
            return True
        return _dorossi_offline_marker_in(f"{type(exc).__name__}: {exc}") and \
            getattr(exc, "status_code", None) in (None, "")
    except Exception:  # pylint: disable=broad-except
        return False


def _dorossi_cc_transient_error(result_ev: dict, answer: str = "",
                                session_id: str | None = None
                                ) -> "_DorossiTransientError | None":
    """看 `result` 事件像不像「伺服器暫時性故障」；是就回一個填好的例外，否則 None。

    與 `_dorossi_cc_usage_limit` 同一個形狀，**而且必須排在它後面**呼叫：用量上限
    也可能帶著 5xx 以外的狀態碼，但它有專屬的等待策略（等到額度重設），比這裡的
    指數退避精準得多。
    """
    if not isinstance(result_ev, dict):
        result_ev = {}
    status = result_ev.get("api_error_status")
    try:
        status_int = int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        status_int = None
    haystack = " ".join(
        str(result_ev.get(k) or "")
        for k in ("subtype", "result", "terminal_reason")
    )
    if answer:
        haystack += " " + str(answer)
    low = haystack.lower()
    hit = (status_int in DOROSSI_TRANSIENT_STATUSES
           or any(marker in low for marker in _TRANSIENT_TEXT_MARKERS))
    if not hit:
        return None
    return _DorossiTransientError(
        (haystack.strip() or f"api_error_status={status_int}")[:400],
        status=status_int, session_id=session_id)


def _dorossi_transient_wait_seconds(attempt: int, *, base: float = 30.0,
                                    cap: float = 900.0) -> float:
    """第 `attempt` 次（從 1 起算）連續暫時性失敗要等幾秒：指數退避、上限 `cap`。

    起步 30 秒而不是幾秒：伺服器過載時立刻重打只會加重它，而無人值守的任務並不
    急著在十秒內恢復。上限 15 分鐘，讓一次長時間的服務中斷不會變成每 15 分鐘之外
    的空轉，也不會久到錯過恢復。
    """
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return float(min(cap, base * (2 ** (n - 1))))


class _DorossiLoopSilence(RuntimeError):
    """Raised when an autonomous-loop round is cut because there was no new
    output for the configured silence window. This is the loop's per-round
    backstop and fires even while a tool is still 'executing' (unlike the normal
    idle tier, which waits for pending tools) — so a hung tool in an unattended
    loop can't suppress the watchdog forever.

    2026-09-05 起迴圈**會先重生幾次再放棄**（`dorossi_silence_retry_max`，預設 2，
    退避 20s→40s）：卡住的多半是那個後端行程，換一個新的常常就過了，而在那之前
    一次卡頓就等於整個無人值守任務停在原地等人接。連續卡到超過上限才停——那時候
    就不像偶發了。設成 0 會回到「一次靜默就停」的舊行為。"""


class _DorossiUsageLimitError(RuntimeError):
    """Raised when a Dorossi turn fails because the underlying plan / quota usage
    limit was hit (not a transient or stale-session failure, so the fresh-session
    retry must be skipped).

    三個欄位刻意分成「給人看的」與「給程式用的」兩類，不要合併：

    * `reset_hint` — **給人看的**重設時間字串，已經過 `_dorossi_sanitize_reset_hint`
      收斂成已知安全形狀（它是本模組唯一會被組進 Discord 回覆的後端原始文字）。
      不可信、不可拿來算數；`None` 代表沒有可展示的提示。
    * `reset_at` — **給程式用的** epoch 秒數，只有在後端給出明確時間戳
      （`…|<epoch>` 變體、或 API 後端的 `retry-after` 標頭）時才有值。自走迴圈用它
      決定要睡多久；拿不到就 `None`，由呼叫端退避探測。**人類可讀的
      「resets 3:45pm」不會被換算成 `reset_at`**——那種寫法沒有時區、猜錯 5 小時的
      代價遠大於多探一次的成本，理由寫在 `_dorossi_extract_reset_epoch`。
    * `session_id` — 撞上上限那一刻後端已經推進到的工作階段 id（可能為 `None`）。
      **這是「等待後續跑不會丟掉工作」的關鍵**：用量上限通常是「做到一半」才撞上，
      若不把這個 id 存回 slot，等額度回來之後 resume 的會是上一輪的舊 id，這一輪
      已經做完的事就全部白做。
    """

    def __init__(self, message: str, reset_hint: str | None = None, *,
                 reset_at: float | None = None,
                 session_id: str | None = None) -> None:
        super().__init__(message)
        self.reset_hint = reset_hint
        self.reset_at = reset_at
        self.session_id = session_id


class _DorossiWorkdirError(NotADirectoryError):
    """spawn 前發現工作目錄已經不是可用的目錄（`_dorossi_require_workdir` 丟的）。

    **刻意繼承 `NotADirectoryError`，不像兄弟們繼承 `RuntimeError`。** 修正前這個狀況
    就是子行程丟的 `NotADirectoryError`，而 `dorossi_error_is_fatal` 已經把那個型別判成
    致命（自走迴圈立刻停，不白白重試三輪）；任何 `except OSError` 也照舊接得到。這裡只是
    提早、帶名字地丟出同一件事，讓 `_dorossi_error_hint` 分得出來、講對原因。改成
    `RuntimeError` 的話，一個永久的狀況會被迴圈重試三輪才停。

    訊息是固定的英文短句、不含路徑；它不會被送到對話平台（對外字串由 bot 組）。
    """


class _DorossiCliOptionError(RuntimeError):
    """後端 CLI **在開始這一輪之前**就拒絕了我們傳給它的某個選項（`unknown option`）。

    幾乎一定是「裝著的 CLI 比這份程式碼舊」：本專案會隨 CLI 的新功能加旗標（例如
    2026-09-19 純聊天加的 `--tools ""`），而舊版的指令列剖析器看到不認得的選項就以
    rc=1 結束、stdout 一個字都沒有、stderr 是 `error: unknown option '<旗標>'`（同日用
    2.1.276 餵一個不存在的選項實測）。

    分出一個型別有兩個理由：(1) 不要落到 resume 重試——重開一個工作階段會以完全相同
    的方式失敗，只是多起一次行程、多印一次看不懂的 log；(2) 重試毫無機會成功，所以
    列在 `_FATAL_ERROR_TYPES`，無人值守的迴圈不必白試三輪。
    `option` 是被拒絕的那個旗標名（已由 `_CLI_UNKNOWN_OPTION_RE` 收斂成旗標的形狀）。
    訊息是固定英文加旗標名、不含路徑；對外字串由 bot 組（一律泛用）。
    """

    def __init__(self, option: str) -> None:
        super().__init__(f"claude -p does not accept the option {option!r}")
        self.option = option


class _DorossiAuthError(RuntimeError):
    """後端 CLI **沒有可用的登入**：沒登入、憑證失效、或被迫進 bare 模式而讀不到登入。

    2026-09-19 補。實測（CLI 2.1.276）兩種形狀都是 rc=1 ＋ 一則 `is_error` 為真的
    result，而在這之前它們落到最後兩條：有工作階段 → `_DorossiResumeError` → 呼叫端
    **丟掉對話**重開一個（以完全相同的方式失敗）→ `RuntimeError`，而那句文字（「Not
    logged in · Please run /login」「Failed to authenticate. API Error: 401 …」）一個
    `_FATAL_ERROR_MARKERS` 都不中，所以自走迴圈再重試三輪（20／40／80 秒退避）。401
    那一種每一次叫用都要先讓 CLI 自己重試十次（實測 190 秒），一輪是 resume ＋ 新開
    兩次。沒有一次有機會成功：登入不會在重試之間自己回來。

    列在 `_FATAL_ERROR_TYPES`（重試無用），而且判定排在 resume 重試**之前**（丟掉對話
    也無用）。`evidence` 是判定依據的標籤（`api_error_status=401`、
    `api_retry=authentication_failed`、`result text`），由判定端用固定詞彙組成、不含
    CLI 的原始文字；`bare_suspect` ＝ init 事件沒有 `memory_paths`（像 bare 模式）。

    訊息刻意**不含** `_FATAL_ERROR_MARKERS` 的任何字樣：致命判定必須靠型別成立，不能靠
    訊息剛好含某個字——否則把它從 `_FATAL_ERROR_TYPES` 拿掉也不會有任何測試變紅。
    對外字串由 bot 組（一律泛用）。
    """

    def __init__(self, evidence: str, *, bare_suspect: bool = False) -> None:
        super().__init__(f"claude -p has no usable sign-in ({evidence})")
        self.evidence = evidence
        self.bare_suspect = bare_suspect


# --- Multi-session store --------------------------------------------------
# The on-disk store is `{uid: <user-record>}` where each user record is:
#   {"active": "<sid>" | None,    # currently-selected session id
#    "next_seq": <int>,           # next per-user counter → id "s<next_seq>"
#    "sessions": {"<sid>": {cc_session_id?, cc_cwd?, cc_extra_dir?,
#                           api_history?, label?, created_at?, last_used?}}}
# Session ids are "s1", "s2", … (per-user incrementing; a deleted number is NOT
# reused within the user's lifetime). The legacy flat record (a bare session
# dict with no "sessions" key) is auto-migrated to this shape on load, wrapped
# as session "s1" so the user's existing conversation survives unchanged.
_DOROSSI_SESSION_ID_RE = re.compile(r"^s(\d+)$")
# A session label is user-supplied free text shown back in the list. Sanitize
# (strip newlines/backticks, cap length) so it can't break the rendered list.
DOROSSI_SESSION_LABEL_MAX = 50


def _dorossi_is_session_id(token: str) -> bool:
    """True iff `token` is a session id (`s` followed by digits). Reserved word
    `new` never matches, so it can't collide with an id."""
    return bool(token) and bool(_DOROSSI_SESSION_ID_RE.match(token))


def _dorossi_session_sort_key(sid: str):
    """Sort ids by their numeric part (s2 < s10); non-ids sort last."""
    m = _DOROSSI_SESSION_ID_RE.match(sid)
    return (0, int(m.group(1))) if m else (1, sid)


def _dorossi_clean_label(raw: str | None) -> str | None:
    """Normalize a user-provided label for safe display, or None if blank."""
    if not raw:
        return None
    cleaned = raw.replace("`", "'").replace("\n", " ").replace("\r", " ").strip()
    if not cleaned:
        return None
    return cleaned[:DOROSSI_SESSION_LABEL_MAX]


def _dorossi_parse_session_new(tail: str | None) -> tuple[str | None, str | None]:
    """把 `/dorossi session new` 的參數切成 `(label, cwd)`（純函式，永不 raise）。

    語法：`new [標籤] [cwd=<目錄>]`
      * 標籤是自由文字，可含空格、`,`、`(`、`:` ——所以工作目錄只認 `cwd=`
        （大小寫不拘）這個鍵，不能靠位置切。
      * `cwd=` **之後整段**都是路徑：路徑本身可含空格、`:`、`\\`，一律原樣保留、
        永不 lower()。因此標籤要寫在 `cwd=` 前面。
      * 這裡**不驗證**路徑；呼叫端用 `_dorossi_validate_dir` 驗「已存在的目錄」
        才套用，被拒的原始字串只寫 stderr（對外永遠不回傳路徑）。

    刻意只認 `cwd=`：`dir=`（`--add-dir` 的額外可存取範圍）在 `_dorossi_parse_reset`
    是另一個語意，不併進這條文法——額外範圍請用 `/dorossi allowdir add`。
    """
    text = (tail or "").strip()
    if not text:
        return None, None
    idx = text.lower().find("cwd=")
    if idx == -1:
        return (text or None), None
    label = text[:idx].strip()
    cwd = text[idx + len("cwd="):].strip()
    return (label or None), (cwd or None)


def _dorossi_empty_user() -> dict:
    """A fresh user record with no sessions yet."""
    return {"active": None, "next_seq": 1, "sessions": {}}


def _dorossi_int_or_none(value):
    """`value` 是真正的 int 就回它，否則 None。

    **`bool` 要單獨排掉**：它是 `int` 的子類別，`True` 會一路變成 1，於是
    `f"s{seq}"` 產出 `"sTrue"`——一個 `_DOROSSI_SESSION_ID_RE` 認不得的 id。
    `CLAUDE.md` 對兩個設定載入器寫過同一條規則，這裡是第三處。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _dorossi_derive_next_seq(sessions: dict) -> int:
    """Next counter value that is strictly greater than every existing id, so a
    rebuilt counter never re-issues a live id."""
    mx = 0
    for sid in sessions or {}:
        m = _DOROSSI_SESSION_ID_RE.match(str(sid))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _dorossi_migrate_user(rec) -> dict:
    """Normalize one user record to the multi-session shape. A legacy flat dict
    (no "sessions" key) is wrapped as session "s1" so its conversation survives;
    garbage becomes an empty user. Never raises (caller guards too).

    正規化時也會丟掉 `sessions` 裡**畸形的 slot**——值不是 dict（或 key 不是字串）
    的條目。`dorossi_session.json` 是本機檔案、擁有者自己編輯得動，而每一個讀取端
    （工作階段清單、每一輪問答取的快照、匯出）都直接對 slot 呼叫 `.get(...)`，
    所以一筆手改出來的畸形 slot 會讓那些指令全部炸 AttributeError——而那正是擁有者
    最需要它們的時候。

    攔在這裡而不是各讀取端各防一次，理由是這裡是整個存放檔的**單一正規化入口**：
    `_dorossi_load_state` 一定走 `_dorossi_migrate_state`，後者對每個 uid 呼叫本
    函式。攔一次，所有讀取端一起受惠；分散防守則會漏掉下一個新增的讀取端，而漏掉
    的那一個不會有任何症狀，直到有人真的手改過那個檔案。

    丟棄發生在修補 `active` / `next_seq` **之前**，所以 `active` 不會指到一個剛被
    丟掉的 id（既有的 `if active not in sessions` 順序對了就自然正確）。正常資料的
    輸出完全不變：沒有畸形 slot 時連 `sessions` 這個 dict 物件都原樣沿用、不重建。
    """
    if not isinstance(rec, dict):
        return _dorossi_empty_user()
    sessions = rec.get("sessions")
    if isinstance(sessions, dict):
        bad = [sid for sid, sess in sessions.items()
               if not isinstance(sid, str) or not isinstance(sess, dict)]
        if bad:
            sessions = {sid: sess for sid, sess in sessions.items()
                        if isinstance(sid, str) and isinstance(sess, dict)}
            # 只印形狀像工作階段 id 的 key。其餘 key 來自手改過的檔案，可能裝著
            # 任何東西（主機路徑、提示詞片段），而這行 stderr 會進 discord_bot.log，
            # 再由 log 查詢指令送進聊天平台——中間只有一個靠樣式比對的 scrubber，
            # 認不得沒見過的形狀。slot 的內容一律不印。
            shown = sorted(sid for sid in bad
                           if isinstance(sid, str)
                           and _DOROSSI_SESSION_ID_RE.match(sid))
            note = ",".join(shown) if shown else "(none)"
            if len(shown) != len(bad):
                note += f" +{len(bad) - len(shown)} withheld"
            print(f"[dorossi] dropped {len(bad)} malformed session slot(s); "
                  f"ids={note}", file=sys.stderr)
        # Already new shape — repair active/next_seq defensively.
        next_seq = _dorossi_int_or_none(rec.get("next_seq"))
        if next_seq is None or next_seq <= _dorossi_derive_next_seq(
                sessions) - 1:
            next_seq = max(_dorossi_derive_next_seq(sessions),
                           next_seq if next_seq is not None else 1)
        active = rec.get("active")
        if active not in sessions:
            active = None
        return {"active": active, "next_seq": next_seq, "sessions": sessions}
    # Legacy flat record → wrap the whole dict as session "s1" (preserve it).
    return {"active": "s1", "next_seq": 2, "sessions": {"s1": dict(rec)}}


def _dorossi_migrate_state(state) -> dict:
    """Normalize the whole store to the multi-session shape in place. Robust to a
    missing/corrupt file or any garbage entry (never raises)."""
    if not isinstance(state, dict):
        return {}
    for uid in list(state.keys()):
        try:
            state[uid] = _dorossi_migrate_user(state.get(uid))
        except Exception:  # pylint: disable=broad-except
            state[uid] = _dorossi_empty_user()
    return state


def _dorossi_user_record(state: dict, uid: str) -> dict:
    """Return the uid's user record (multi-session shape), creating an empty one
    if absent/malformed. The returned dict is the live slot inside `state`."""
    rec = state.get(uid)
    if not isinstance(rec, dict) or not isinstance(rec.get("sessions"), dict):
        rec = _dorossi_empty_user()
        state[uid] = rec
    return rec


def _dorossi_new_session(rec: dict, label: str | None = None) -> str:
    """Allocate a new session under `rec`, mark it active, return its id. Uses
    the per-user counter (deleted numbers are NOT reused); defensively skips any
    id that somehow already exists."""
    seq = _dorossi_int_or_none(rec.get("next_seq"))
    if seq is None or seq < 1:
        seq = _dorossi_derive_next_seq(rec.get("sessions") or {})
    sessions = rec.setdefault("sessions", {})
    sid = f"s{seq}"
    while sid in sessions:
        seq += 1
        sid = f"s{seq}"
    now = time.time()
    sess: dict = {"created_at": now, "last_used": now}
    if label:
        sess["label"] = label
    sessions[sid] = sess
    rec["next_seq"] = seq + 1
    rec["active"] = sid
    return sid


def _dorossi_active_session(state: dict, uid: str) -> tuple[str, dict]:
    """Return (session_id, session_dict) for the uid's active session, creating
    a fresh active session (and the user record) if there is none. The returned
    dict is the live slot inside `state` — mutate it then `_dorossi_save_state`;
    never write `state[uid] = sess` (that would clobber the user record)."""
    rec = _dorossi_user_record(state, uid)
    sessions = rec["sessions"]
    active = rec.get("active")
    if active not in sessions:
        sid = _dorossi_new_session(rec)
        return sid, rec["sessions"][sid]
    return active, sessions[active]


def _dorossi_session_by_id(state: dict, uid: str, sid: str) -> tuple[str, dict]:
    """Return (session_id, session_dict) for a SPECIFIC pre-resolved slot id. Used
    by the parallelised turn path, which resolves the target slot once at dispatch
    (under the state short-lock) and re-fetches it on each state read-modify-write
    so a fresh `state` reload doesn't carry a stale slot reference across a backend
    call. If the slot vanished (deleted between dispatch and now — should not
    happen while the turn holds the session lock, but guard anyway) we fall back to
    the active session so the turn still has a live slot to operate on. The
    returned dict is the live slot inside `state`; mutate it then
    `_dorossi_save_state` — never write `state[uid] = sess`."""
    rec = _dorossi_user_record(state, uid)
    sessions = rec["sessions"]
    if sid in sessions:
        return sid, sessions[sid]
    return _dorossi_active_session(state, uid)


def _dorossi_reset_session(sess: dict) -> None:
    """Drop the backend continuity (session id, scoped dirs, api history), the
    session-scoped tuning overrides (`tune_effort`/`tune_model` — a reset/new
    context starts from the defaults, owner ruling) AND any unfinished-loop
    marker (`loop_pending` — a reset context has nothing to resume) so the next
    turn starts fresh, keeping the slot's id/label/created_at. In place.
    `cc_usage_mark` (the cumulative-usage baseline of the dropped backend session,
    see `_dorossi_cc_account_round`) goes too: it is keyed to that session id and
    would never match again."""
    for k in ("cc_session_id", "codex_session_id", "cc_cwd", "cc_extra_dir", "api_history",
              "tune_effort", "tune_model", "loop_pending", "cc_usage_mark"):
        sess.pop(k, None)
    sess["last_used"] = time.time()


def _dorossi_mark_loop_pending(sess: dict, task: str, *,
                               channel_id=None, message_id=None) -> None:
    """在 session slot 記下「有未完成的自走任務」（in place；自走迴圈啟動時呼叫，
    乾淨收尾時用 _dorossi_clear_loop_pending 清掉）。任務描述留存供之後「fresh 重跑」
    與列表顯示；新描述為空時保留舊描述（接續同一任務不清掉原始任務文字）。任何
    中斷路徑（abort／沉默 backstop／用量上限／例外／bot 重啟）都不會清掉這個標記，
    所以擁有者事後可用 `@bot session <id> continue` 接續。純函式、永不 raise。

    另外記三件給「跨重啟自動接續」用的東西：

    * `live` ── 「此刻有一個行程正在跑這個迴圈」。設為 True 的地方只有這裡；改回
      False 的地方只有 `_dorossi_mark_loop_stopped`，而那個**只從迴圈的 `finally`
      呼叫**。`finally` 在自願結束（abort／沉默／例外／放棄）時一定會跑，行程被砍
      時一定不會跑——所以「重啟後看到 live 還是 True」精確等於「上一個行程是被砍死
      的，不是自己停的」。這正是自動接續要的判準：擁有者按了 abort 就不該被自動
      接回去。
    * `channel_id` / `message_id` ── 接續時要用的錨點。重啟後向平台查回紀錄當
      `_dorossi_run_loop` 的 `message`，**擁有者閘門是用平台說的發起人重驗的**，不是
      信任這裡存的 id。⚠️ `message_id` 對斜線指令來說是 **interaction id**，不是訊息 id
      ——bot 那一側的 `_resolve_trigger_message` 會從 bot 自己那則回覆的
      `interaction_metadata` 找回發起人（2026-09-19 以前直接抓訊息、必定 404，這個功能
      因此從來沒成功過）。查不回來就不自動接，標記留著給人工接。
    * `auto_tries` ── 連續自動接續次數，當機迴圈的斷路器；沿用舊值（接續同一任務
      不歸零，歸零只在真的跑完一輪時，見 `_dorossi_touch_loop_pending`）。
    """
    prev = sess.get("loop_pending")
    prev = prev if isinstance(prev, dict) else {}
    text = (task or "").strip() or prev.get("task", "")
    marker = {"ts": time.time(), "task": text[:2000], "live": True}
    for key, value in (("channel_id", channel_id), ("message_id", message_id)):
        keep = value if isinstance(value, int) and not isinstance(value, bool) \
            else prev.get(key)
        if isinstance(keep, int) and not isinstance(keep, bool):
            marker[key] = keep
    tries = prev.get("auto_tries")
    if isinstance(tries, int) and not isinstance(tries, bool) and tries > 0:
        marker["auto_tries"] = tries
    sess["loop_pending"] = marker


# 自走迴圈停下來的原因（`loop_pending["stop"]`，由迴圈的 `finally` 寫）。只有
# 「網路斷了」與「被取消」（bot 關機／重連時 task 被取消）算**不是自己要停的**，
# 會被自動接續；abort 永遠不會（擁有者明確表達過的意圖，不能被自動化推翻）。
# 舊標記沒有這個鍵：`live` 為假就當成原因不明，自動接續照舊不接。
DOROSSI_LOOP_STOP_REASONS = frozenset({
    "abort",        # 擁有者 `/dorossi abort`
    "network",      # 平台連線中斷，迴圈沒辦法再說話
    "interrupted",  # task 被取消（bot 關機、重連時被收掉）
    "silence",      # 輸出沉默重試用完
    "usage",        # 用量等待次數到了設定的保底上限
    "transient",    # 暫時性故障重試用完
    "error",        # 其他錯誤（含致命錯誤）
    "deleted",      # slot 被刪了（標記跟著不在，寫不進去，留著只為完整）
})
_DOROSSI_AUTORESUMABLE_STOPS = frozenset({"network", "interrupted"})


def _dorossi_touch_loop_pending(sess: dict, *, reset_tries: bool = False) -> None:
    """把標記的心跳推到現在（in place）。**絕不建立標記**——沒有標記就什麼都不做，
    否則「已自然收尾、標記已清掉」的 slot 會被心跳復活成「有未完成任務」。

    心跳存在的理由：`ts` 若只在迴圈啟動時寫一次，一個跑了三天的任務在重啟後就會
    因為「標記太舊」被拒絕自動接續——而那正是這個功能最該接的情形。跑完一輪
    （`reset_tries=True`，同時清掉當機迴圈計數）與進入用量等待前各推一次，所以
    `ts` 的語意是「這個迴圈最後一次被證實還活著的時刻」。永不 raise。"""
    marker = sess.get("loop_pending")
    if not isinstance(marker, dict):
        return
    marker["ts"] = time.time()
    marker["live"] = True
    marker.pop("stop", None)   # 還活著就沒有「停下來的原因」
    if reset_tries:
        marker.pop("auto_tries", None)


def _dorossi_mark_loop_stopped(sess: dict, reason: str | None = None) -> None:
    """標記「迴圈是自己停下來的，不是被砍死的」（in place）。**只從自走迴圈的
    `finally` 呼叫**，理由見 `_dorossi_mark_loop_pending` 的 `live` 說明。
    絕不建立標記（自然收尾那條路已經把整個標記清掉了，不要復活它）。永不 raise。

    `reason`（`DOROSSI_LOOP_STOP_REASONS` 之一）記在 `stop`：只看 `live` 分不出「擁有者
    按了 abort」與「網路斷了」，而後者正是該被接回去的（2026-09-22 的斷網把六個任務都
    寫成 `live=False`，於是沒有一個被接回去）。認不得的值不寫——沒有原因就等同舊標記。"""
    marker = sess.get("loop_pending")
    if isinstance(marker, dict):
        marker["live"] = False
        if reason in DOROSSI_LOOP_STOP_REASONS:
            marker["stop"] = reason
            marker["stopped_ts"] = time.time()
        else:
            marker.pop("stop", None)


def _dorossi_mark_loop_aborted(sess: dict) -> None:
    """擁有者對一個**沒在跑、但等著被自動接續**的任務按了 abort（in place）。

    迴圈在跑的時候，abort 由迴圈自己的 `finally` 寫成 `stop: abort`；這一支給「迴圈
    已經因斷網停下來、連線回來就會被接回去」的那種——不寫的話，擁有者中止了也沒用，
    網路一回來它就自己活過來。絕不建立標記。永不 raise。"""
    marker = sess.get("loop_pending")
    if isinstance(marker, dict):
        marker["live"] = False
        marker["stop"] = "abort"
        marker["stopped_ts"] = time.time()


def _dorossi_clear_loop_pending(sess: dict) -> None:
    """清掉「未完成自走任務」標記（自走迴圈自然收尾——連續無進展而停止——時呼叫）。"""
    sess.pop("loop_pending", None)


def _dorossi_loop_marker_wants_autoresume(marker) -> bool:
    """標記本身說「這個任務不是自己要停的」：`live` 還是 True（行程被砍），或停下來的
    原因在 `_DOROSSI_AUTORESUMABLE_STOPS`。abort 與舊標記（沒有原因、`live` 為假）一律
    否。純函式、永不 raise。"""
    if not isinstance(marker, dict):
        return False
    if marker.get("live") is True:
        return True
    return marker.get("stop") in _DOROSSI_AUTORESUMABLE_STOPS


def _dorossi_loop_autoresume_plan(sess: dict, *, now=None):
    """bot 起來時要不要自己把這個 slot 的自走任務接回去？（純函式、永不 raise）

    回傳 `(channel_id, message_id, tries)` 或 None。四道條件全過才回非 None：

    1. 有形狀正確的 `loop_pending`，且 `live` 為 True——亦即上一個行程是被砍死的；
       **或**迴圈自己停下來、但原因是 `_DOROSSI_AUTORESUMABLE_STOPS`（網路斷了、task
       被取消）。擁有者自己 abort／沉默 backstop／例外／放棄都不在其中。
    2. 有 `channel_id` 與 `message_id` 錨點（舊版標記沒有這兩個鍵，於是只能人工
       接續——這是刻意的向後相容行為，不是漏洞）。
    3. 心跳在 `DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC` 以內（0 ＝功能關閉）。時鐘倒退
       導致的「未來心跳」一律視為過期，不給負數年齡矇混過關。
    4. `auto_tries` 還沒到 `DOROSSI_LOOP_AUTORESUME_MAX_TRIES`（0 ＝不設限）。

    **這裡不做權限判斷。** 擁有者閘門一律由呼叫端拿「真的抓回來的那則訊息」重驗，
    存在磁碟上的 id 不是授權依據。"""
    max_age = DOROSSI_LOOP_AUTORESUME_MAX_AGE_SEC
    if not isinstance(max_age, (int, float)) or isinstance(max_age, bool) \
            or max_age <= 0 or max_age != max_age:  # NaN 也當關閉
        return None
    marker = sess.get("loop_pending")
    if not _dorossi_loop_marker_wants_autoresume(marker):
        return None
    cid, mid = marker.get("channel_id"), marker.get("message_id")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
               for v in (cid, mid)):
        return None
    ts = marker.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts != ts:
        return None
    age = (time.time() if now is None else now) - ts
    if not 0 <= age <= max_age:
        return None
    tries = marker.get("auto_tries")
    tries = tries if isinstance(tries, int) and not isinstance(tries, bool) \
        and tries > 0 else 0
    cap = DOROSSI_LOOP_AUTORESUME_MAX_TRIES
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 \
            and tries >= cap:
        return None
    return (cid, mid, tries)


def _dorossi_count_autoresume(sess: dict) -> None:
    """把這個 slot 的「連續自動接續次數」加一（in place）。在**真的起跑之前**呼叫，
    所以即使接續當場又把 bot 弄死，計數也已經落地了——這正是斷路器要的。
    絕不建立標記。永不 raise。"""
    marker = sess.get("loop_pending")
    if not isinstance(marker, dict):
        return
    tries = marker.get("auto_tries")
    tries = tries if isinstance(tries, int) and not isinstance(tries, bool) \
        and tries > 0 else 0
    marker["auto_tries"] = tries + 1


def _dorossi_loop_resume_plan(sess: dict):
    """判斷一個 session slot 有沒有「可接續的自走任務」、以及該怎麼接（純函式）。

    回傳：
      * None ── 沒有可接續的任務（沒有 loop_pending 標記，或標記形狀不對）。
      * ("continue", None) ── 後端脈絡還在（有 cc_session_id）：resume 同一工作
        階段、以 CONTINUE 提示接續，最完整（自走迴圈 already_ran_first=True 路徑）。
      * ("fresh", task) ── 脈絡已不在（session 被清）但留有任務描述：用原任務文字
        重新起跑（already_ran_first=False 路徑）。
    永不 raise；store 被手改成怪形狀一律當「沒有可接續的」。"""
    pending = sess.get("loop_pending")
    if not isinstance(pending, dict):
        return None
    if sess.get("cc_session_id") or sess.get("codex_session_id"):
        return ("continue", None)
    task = pending.get("task")
    if isinstance(task, str) and task.strip():
        return ("fresh", task.strip())
    return None


def _dorossi_session_is_stale(sess: dict) -> bool:
    """True when an existing session hasn't been used for longer than the
    configured max age (conservative single-turn hygiene). 0/disabled, a missing
    timestamp, or a clock skew (last_used in the future) → never stale. Bounds an
    unbounded long-lived session WITHOUT forcing the user to reset manually; the
    caller only acts when the slot actually has continuity to drop."""
    if DOROSSI_SESSION_MAX_AGE_DAYS <= 0:
        return False
    last = sess.get("last_used") or sess.get("created_at")
    if not isinstance(last, (int, float)) or isinstance(last, bool) or last <= 0:
        return False
    age_days = (time.time() - last) / 86400.0
    return age_days >= DOROSSI_SESSION_MAX_AGE_DAYS


def _dorossi_most_recent_session(sessions: dict) -> str | None:
    """Id of the most-recently-used remaining session (by last_used, then
    created_at), or None when there are none. Used to re-point `active` after a
    delete."""
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda sid: (sessions[sid].get("last_used")
                         or sessions[sid].get("created_at") or 0))


def _dorossi_load_state() -> dict:
    """Load the per-user session store, migrated to the multi-session shape.
    Never raises — a missing/corrupt file just means 'no sessions yet'."""
    try:
        text = DOROSSI_SESSION_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        # 暫時性 I/O 問題（鎖住／權限）：什麼都不動，這次當成「還沒有工作階段」。
        print(f"[dorossi] session load failed: {exc!r}", file=sys.stderr)
        return {}
    try:
        raw = _json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        # 內容毀損（不是暫時性 I/O 問題）：先把壞檔搬到 .bad 留存再回空狀態。若原樣
        # 留著，下一次 _dorossi_save_state 會以「空 state」整檔覆寫掉它（存檔是
        # temp+os.replace 的全檔取代），使用者所有工作階段就此永久消失、連手動救回
        # 的機會都沒有。搬檔本身同樣 fail-soft，失敗只記 stderr。
        print(f"[dorossi] session file corrupt, quarantining: {exc!r}",
              file=sys.stderr)
        try:
            os.replace(DOROSSI_SESSION_FILE,
                       DOROSSI_SESSION_FILE.with_name(
                           DOROSSI_SESSION_FILE.name + ".bad"))
        except Exception as exc2:  # pylint: disable=broad-except
            print(f"[dorossi] session quarantine failed: {exc2!r}",
                  file=sys.stderr)
        return {}
    return _dorossi_migrate_state(raw)


def _dorossi_save_state(state: dict) -> None:
    """Persist the session store atomically (temp + os.replace)."""
    try:
        tmp = DOROSSI_SESSION_FILE.with_name(DOROSSI_SESSION_FILE.name + ".tmp")
        tmp.write_text(_json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DOROSSI_SESSION_FILE)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] session save failed: {exc!r}", file=sys.stderr)


# Markers that identify a plan / quota usage-limit notice in the claude_code
# backend's `result` text (or its `result` event). The headless CLI reports a
# usage limit either as a non-zero exit OR as an rc==0 run whose `result` text
# is actually the limit notice rather than an answer — both are matched here.
# Kept lower-cased; the matcher lower-cases its input. Phrasings cover the
# documented "You've hit your … limit · resets …" line, the generic "usage
# limit reached", explicit rate-limit wording, and the pipe-delimited
# "Claude AI usage limit reached|<epoch>" variant seen in the wild.
_DOROSSI_USAGE_LIMIT_MARKERS = (
    "usage limit reached",
    "usage limit exceeded",
    "hit your usage limit",
    "you've hit your",          # "You've hit your session/weekly/Opus limit"
    "limit reached",
    "rate limit",
    "rate_limit",
    "5-hour limit",
    "five_hour",
    "weekly limit",
    "out of credits",
    "out_of_credits",
)

# 上面那張表**只能用來判「錯誤文字」**，不能拿去判「成功回合的答案」（2026-09-19 修）。
#
# 表比對得很寬（"rate limit"、"limit reached"、"five_hour"），而成功回合的 result 文字
# 是**模型自己寫的散文**。2026-09-17 22:44 與 2026-09-19 05:42 各有一輪 rc==0、
# subtype=success 的正常回答，只因為內文**討論到**某個 SDK 的 rate limit（後者 3892
# 字，命中點在第 2298 字）就被判成用量上限：答案整份丟掉，自走迴圈睡到五小時視窗的
# 重設時刻，白等一小時四十八分。
#
# 真正的上限通知在這台機器的 log（09-03 起）裡出現過 42 次，**全部**是 rc=1、
# is_error=True、api_error_status=429——結構化欄位就足以判定，文字根本用不到。rc==0 的文字路徑是給
# 上游舊版 CLI 那種「result 文字本身就是通知」留的防線，所以在**成功的** result 上，
# 文字證據只在兩個條件都成立時才算數（`_dorossi_cc_limit_text_counts`）：
#
#   1. 串流的 `rate_limit_event` 沒有說「這次呼叫放行了」。伺服器的配額標頭說
#      allowed，這一輪就不可能是配額拒絕——這是結構化的否決。
#   2. 文字**長得像一則通知**：CLI 的通知是一行模板（「You've hit your session limit ·
#      resets 4:50pm (Asia/Taipei)」，那 42 則裡最長的 65 字），CLI 另外會接上「·
#      progress saved」「· ask your admin for a higher limit」這類尾巴，所以上限留到
#      300 字；會討論 rate limit 的答案是幾百、幾千字的散文。
#
# 兩條各擋一種情況，**不要只留一條**：否決只在事件存在時有效（舊版 CLI、API key 登入的
# 工作階段都沒有這個事件），長度則擋不住「短答案剛好提到 rate limit」。
# 錯誤的 result（is_error 為真、subtype 不是 success、或根本沒有 result 事件）照舊用整張
# 表判定：那時的文字是 CLI 的錯誤訊息，不是模型的答案。
_DOROSSI_USAGE_NOTICE_MAX_CHARS = 300
# `rate_limit_event.rate_limit_info.status` 的詞彙。**不是猜的**：取自 CLI 自己的 SDK
# 事件 schema（2.1.276 執行檔內 `status:q(["allowed","allowed_warning","rejected"])`，
# 2026-09-19 查），另有 2026-08-31 實跑收到的 "allowed" 為證
# （`test_dorossi_usage_limit.REAL_RATE_LIMIT_EVENT`）。前兩個代表這次呼叫被放行
# （allowed_warning ＝快到上限、但還是放行），只有這兩個能否決文字判定。
_DOROSSI_RATE_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
_DOROSSI_RATE_STATUS_ALLOWED = frozenset({"allowed", "allowed_warning"})


# `reset_hint` 是本模組**唯一**會被組進 Discord 回覆的後端原始文字
# （_dorossi_usage_limit_reply 會貼成「用量預計於 <hint> 重設」），所以它必須被當成
# 不可信輸入處理。危險點在於 _DOROSSI_USAGE_LIMIT_MARKERS 比對得很寬（"rate limit"、
# "limit reached"…），一段其實是別的錯誤、只是剛好含 "reset" 的後端文字也會走到這裡，
# 例如 "rate limit… connection reset by peer while writing D:\\…\\x.log"——原樣回傳就
# 把主機路徑／服務名／原始例外送進 Discord，違反「不得出現服務名／本機路徑／原始
# 錯誤」硬需求。因此只接受「reset(s) ＋ 短短時間字樣」這種已知安全形狀，其餘一律丟棄
# （回 None，呼叫端就只回泛用的用量上限通知，功能不受影響）。
# 字元集刻意不含 `/`、`\\`、`~`、引號、反引號等路徑／URL 常見字元。
_DOROSSI_RESET_HINT_MAX = 40
_DOROSSI_RESET_HINT_RE = re.compile(r"^resets?\b[A-Za-z0-9 :,.+-]*",
                                    re.IGNORECASE)


def _dorossi_sanitize_reset_hint(snippet: str) -> str | None:
    """把從後端文字擷取出的「重設時間」片段收斂成已知安全形狀，否則回 None。
    純函式、永不 raise（見上方註解的保密理由）。

    做法是**取白名單字元的最長前綴**，不是「整段符合才收」。原本的全有全無版本會
    把上游最常見的那一句整個丟掉——「Your limit will reset at 1pm (Etc/GMT+5)」的
    括號不在字元集裡，於是擁有者一個時間都看不到。前綴版一樣安全（前綴本身完全落在
    白名單內，夾不進路徑或網址），只是不會為了一個括號放棄整句。

    截斷之後還有兩道：

    * `[A-Za-z]:` ── 字母緊接冒號代表磁碟機字首（`D:`）或 scheme（`http:`）。時間裡
      的冒號前面一定是數字。這道是 `rate limit… connection reset by peer while
      writing D:\\…` 那類假 hint 的主要殺手。
    * **必須含數字** ── 「重設時間」一定帶數字。少了這條，`reset by peer while
      writing logs` 這種沒有磁碟機字首的散文會原樣被當成時間送出去。截斷版讓這種
      句子更容易「剛好整段合法」，所以這道是配套的，不是額外的潔癖。
    """
    s = (snippet or "").strip()
    if not s:
        return None
    match = _DOROSSI_RESET_HINT_RE.match(s)
    if match is None:
        return None
    s = match.group(0).strip()
    if not s or len(s) > _DOROSSI_RESET_HINT_MAX:
        return None
    # 時間裡的冒號前面一定是數字（3:45pm）；字母＋冒號代表磁碟機字首（D:）或
    # scheme（http:），一律拒收。
    if re.search(r"[A-Za-z]:", s):
        return None
    if not any(ch.isdigit() for ch in s):
        return None
    return s


# 管線分隔的時間戳變體：`Claude AI usage limit reached|1749924000`。這是實務上
# 唯一**機器可讀**的重設時刻來源（上游 issue 標題大量出現這個形狀），所以自走迴圈
# 「睡到額度回來」只信這一條。10 位＝秒、13 位＝毫秒，兩種都收。
_DOROSSI_RESET_EPOCH_RE = re.compile(r"\|\s*(\d{10,13})\b")
# epoch 合理區間（秒）：2001-09 ～ 2096-10。超出就當作「那串數字不是時間戳」。
_DOROSSI_EPOCH_MIN = 1_000_000_000
_DOROSSI_EPOCH_MAX = 4_000_000_000


def _dorossi_extract_reset_epoch(text: str) -> float | None:
    """從用量上限通知裡取出**機器可讀**的重設時刻（epoch 秒），取不到回 None。
    純函式、永不 raise。

    **只認 `…|<epoch>` 這一種形狀，刻意不換算人類可讀的「resets 3:45pm」。**
    理由是時區：上游實際印出的字樣是「Your limit will reset at 1pm (Etc/GMT+5)」，
    那個時區跟本機時區沒有關係，把 `1pm` 當本機時間換算可能整整差好幾個小時。
    猜錯的兩個方向代價不對稱——猜早了只是多送一次會立刻失敗的探測（便宜），猜晚了
    是整個自走任務白白多停數小時（昂貴）。所以拿不到時間戳時一律回 None，讓呼叫端
    走「短等待起跳、指數退避」的探測，而不是相信一個沒有時區的鐘點。
    `reset_hint`（給人看的那個字串）不受影響，照舊會顯示鐘點寫法。
    """
    if not text:
        return None
    try:
        match = _DOROSSI_RESET_EPOCH_RE.search(text)
        if match is None:
            return None
        raw = int(match.group(1))
        # 13 位是毫秒（防禦性：目前實測是秒，但兩種都收才不會哪天靜悄悄失準）。
        secs = raw / 1000.0 if raw >= 1_000_000_000_000 else float(raw)
        if _DOROSSI_EPOCH_MIN <= secs <= _DOROSSI_EPOCH_MAX:
            return secs
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_rate_limit_reset(ev: dict) -> float | None:
    """從 stream-json 的 `rate_limit_event` 取出重設時刻（epoch 秒）。純函式、永不
    raise。

    **這是目前唯一真正可靠的機器可讀來源。** 原本只認通知文字裡的
    `…|<epoch>`——那個形狀來自上游 issue 標題，而 2026-08-31 實測**目前的 CLI
    根本不再輸出它**（在整支執行檔裡搜不到 `Claude AI usage limit reached`）。
    於是那條路等同永遠回 None，每次撞上限都只能走「15 分鐘起跳、每次加倍」的
    退避探測，最壞情況白等好幾個小時。

    現在的 CLI 改成**每一次呼叫**都在串流裡發一個 `rate_limit_event`：

        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1788199200,
            "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.49,
                                             "resetsAt": 1788199200},
                               "seven_day": {...}}}}

    `resetsAt` 是這個視窗真正的重設時刻（來自伺服器的配額標頭），所以撞上限時可以
    **睡到那一刻**而不是猜。優先取頂層的 `resetsAt`——CLI 已經依 `rateLimitType`
    挑好了當下綁住的那個視窗；取不到才退回 `unifiedWindows.five_hour`。

    週上限（`rateLimitType == "seven_day"`）的 `resetsAt` 可能在好幾天後，但呼叫端
    的 `DOROSSI_USAGE_WAIT_MAX_SEC` 會把單次等待截在 6 小時再重探——探測便宜，睡過
    頭才是不可逆的浪費。這裡不做這個判斷，只忠實回報時刻。
    """
    try:
        info = ev.get("rate_limit_info")
        if not isinstance(info, dict):
            return None
        candidates = [info.get("resetsAt")]
        windows = info.get("unifiedWindows")
        if isinstance(windows, dict):
            for name in ("five_hour", "seven_day"):
                win = windows.get(name)
                if isinstance(win, dict):
                    candidates.append(win.get("resetsAt"))
        for raw in candidates:
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            secs = float(raw)
            if secs != secs:  # NaN
                continue
            if secs >= 1_000_000_000_000:  # 毫秒（防禦性，實測是秒）
                secs /= 1000.0
            if _DOROSSI_EPOCH_MIN <= secs <= _DOROSSI_EPOCH_MAX:
                return secs
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_rate_limit_status(ev) -> str | None:
    """`rate_limit_event` 的 `rate_limit_info.status`；不在已知詞彙裡就回 None。
    純函式、永不 raise。

    認不得的值（詞彙哪天擴充、欄位形狀改掉）一律回 None，**不是**當成 allowed：
    這個值唯一的用途是否決文字判定（見 `_dorossi_cc_limit_text_counts`），所以認不得
    的失敗方向必須是「不否決」——退回原本的文字判定，而不是把一則真的通知放過去。
    """
    try:
        info = ev.get("rate_limit_info")
        if not isinstance(info, dict):
            return None
        status = info.get("status")
        if isinstance(status, str) and status in _DOROSSI_RATE_STATUSES:
            return status
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_cc_limit_text_counts(result_ev: dict, answer: str,
                                  stream_status: str | None) -> bool:
    """用量上限比對表的**文字**命中，這一次算不算數。純函式。

    規則寫在 `_DOROSSI_USAGE_NOTICE_MAX_CHARS` 上方；這裡只做三件事：

    * result 不是成功完成（`_claude_result_succeeded` 為假）→ 算數。那時的文字是 CLI
      的錯誤訊息，沿用原本整張表的判定，rc != 0 的行為因此一個字都沒變。
    * 成功完成、而串流說這次呼叫被放行 → 不算數（結構化否決）。
    * 成功完成、沒有放行訊號 → 文字要短得像一則通知才算數。

    長度量的是 `result` 與 `answer` 裡**比較長**的那一個：兩者在實務上是同一段字，
    但只要有一個是長篇散文，這就不是一則通知。
    """
    if not _claude_result_succeeded(result_ev):
        return True
    if stream_status in _DOROSSI_RATE_STATUS_ALLOWED:
        return False
    raw = result_ev.get("result")
    texts = (raw if isinstance(raw, str) else "", answer if isinstance(answer, str) else "")
    return max(len(t.strip()) for t in texts) <= _DOROSSI_USAGE_NOTICE_MAX_CHARS


def _dorossi_extract_reset_hint(text: str) -> str | None:
    """Pull a reset time out of a usage-limit notice, if present. Handles the
    pipe-delimited `…|<epoch-seconds>` variant (rendered as a local time) and
    the human-readable `resets <when>` phrasing. Returns None when nothing
    parseable is found. Never raises."""
    if not text:
        return None
    try:
        # Variant: "Claude AI usage limit reached|1717689600"
        epoch = _dorossi_extract_reset_epoch(text)
        if epoch is not None:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
        # Variant: "… · resets 3:45pm" / "… resets Mon 12:00am"
        low = text.lower()
        idx = low.find("reset")
        if idx != -1:
            snippet = text[idx:idx + 80].strip()
            # Trim at a sentence/line boundary so the hint stays short.
            for sep in ("\n", ". ", "。"):
                cut = snippet.find(sep)
                if cut > 0:
                    snippet = snippet[:cut]
            # 這段是後端原始文字，會被貼進 Discord → 先收斂成已知安全形狀。
            return _dorossi_sanitize_reset_hint(snippet)
    except Exception:  # pylint: disable=broad-except
        return None
    return None


def _dorossi_cc_usage_limit(result_ev: dict, answer: str,
                            session_id: str | None = None,
                            *, stream_reset: float | None = None,
                            stream_status: str | None = None
                            ) -> _DorossiUsageLimitError | None:
    """Inspect a claude_code `result` event (+ its answer text) and return a
    populated _DorossiUsageLimitError when it represents a plan/quota usage
    limit, else None. Checks the structured `api_error_status` (429 / 402) and
    scans `subtype` + the result/answer text for the documented limit markers,
    so it catches BOTH the non-zero-exit failure and the rc==0 run whose answer
    text is itself the limit notice.

    **結構化欄位永遠優先於文字**：429／402 不經過任何文字條件。文字命中則要再過
    `_dorossi_cc_limit_text_counts`——成功完成的回合裡，文字是模型寫的答案，不是 CLI
    的通知（2026-09-19 事故，見 `_DOROSSI_USAGE_NOTICE_MAX_CHARS` 上方）。
    `stream_status` 是這次呼叫最後一則 `rate_limit_event` 的 status。"""
    status = result_ev.get("api_error_status")
    try:
        status_int = int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        status_int = None
    haystack = " ".join(
        str(result_ev.get(k) or "")
        for k in ("subtype", "result", "terminal_reason")
    )
    if answer:
        haystack += " " + answer
    low = haystack.lower()
    text_hit = (any(marker in low for marker in _DOROSSI_USAGE_LIMIT_MARKERS)
                and _dorossi_cc_limit_text_counts(result_ev, answer, stream_status))
    if status_int in (429, 402) or text_hit:
        reset_text = result_ev.get("result") or answer or ""
        reset = _dorossi_extract_reset_hint(reset_text)
        # epoch 走**整個 haystack**（含 subtype / terminal_reason），因為
        # `…|<epoch>` 不保證出現在 `result` 欄位裡；而 `reset_hint` 維持只看
        # result/answer，那條路徑會被原樣貼進 Discord，掃描範圍越窄越好。
        #
        # **串流事件優先於文字。** `stream_reset` 來自這一次呼叫的
        # `rate_limit_event`（伺服器配額標頭），是結構化的；文字裡的 `|<epoch>`
        # 來自上游 issue 標題那個形狀，2026-08-31 實測目前的 CLI 已經不再輸出它。
        # 兩者都取不到時才會回 None，讓呼叫端走退避探測。
        epoch = stream_reset
        if epoch is None:
            epoch = _dorossi_extract_reset_epoch(haystack)
        # 沒有人看得懂的鐘點、但有機器可讀的時刻 → 用時刻補一個給人看的字串，
        # 否則擁有者會收到「會自動續跑」卻不知道大概什麼時候。
        if reset is None and epoch is not None:
            reset = time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
        return _DorossiUsageLimitError(
            haystack.strip()[:300], reset,
            reset_at=epoch,
            session_id=session_id or None)
    return None


# ---- codex（GPT）那一側的用量上限／暫時性故障 ------------------------------
#
# 2026-09-05 補。在這之前 **codex 路徑完全沒有這兩種判定**：`_dorossi_via_codex`
# 的 rc!=0 只會「有 session_id → `_DorossiResumeError`（丟掉工作階段重開一次）→
# 否則 `RuntimeError`」，於是 GPT 撞到用量上限時，迴圈會先白白重開一個新工作階段
# （一樣會撞上），然後把整個無人值守任務判死。Claude 那一側 2026-08-31 就已經改成
# 「等到額度回復再續跑」，codex 這側一直是舊行為。
#
# 這裡刻意**重用** `_DorossiUsageLimitError` / `_DorossiTransientError`：自走迴圈
# 早就知道怎麼等這兩種例外，所以只要讓 codex 路徑丟對的例外，等待與續跑的邏輯
# 一行都不用改。

# 相對時間的寫法（"try again in 2.363s"、"try again in 4 days 2 hours 46 minutes"）。
# 抓 `… in ` 之後**同一行**的一小段窗口，讓下面的單位比對自己去挑。
# 第一版用的是限縮字元集 `[0-9smhd\s.,]*`，看起來很安全，實際上會在 "4 days" 的
# `a` 就停下來——"4 days 2 hours 46 minutes" 只算到 4 天，2 小時 46 分**無聲地
# 消失**。窗口版安全性一樣（單位比對要求「數字＋時間單位字」，訊息裡的
# "Limit 200000, Used 162582" 沒有單位不會被誤抓），但不會漏掉字詞寫法。
_RETRY_AFTER_RE = re.compile(
    r"(?:try\s+again|retry|resets?)\s+(?:again\s+)?in\s+([^\n]{0,60})",
    re.IGNORECASE)
_DURATION_UNIT_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}


def _dorossi_extract_retry_after_seconds(text: str) -> float | None:
    """從「**相對**」的重試提示裡取出秒數；取不到回 None。純函式、永不 raise。

    與 `_dorossi_extract_reset_epoch` 的取捨相反，而且理由是同一個：那支拒絕換算
    「resets 3:45pm」是因為**沒有時區**，猜錯可能整整差好幾小時。相對寫法沒有這個
    問題——「in 4 days 2 hours」不管在哪個時區都是同一段長度，所以換算是安全的。
    codex 的上限通知用的正是相對寫法。

    只認 `try again in …` / `retry in …` / `resets in …` 之後緊接的那一段，不去掃
    整句裡任何看起來像時間的東西——訊息裡常常還有別的數字（Limit 200000、
    Used 162582），亂抓會得到荒謬的等待長度。
    """
    if not text:
        return None
    match = _RETRY_AFTER_RE.search(str(text))
    if not match:
        return None
    total = 0.0
    for value, unit in _DURATION_UNIT_RE.findall(match.group(1)):
        try:
            total += float(value) * _UNIT_SECONDS[unit[0].lower()]
        except (ValueError, KeyError):
            continue
    if total <= 0:
        return None
    # 上緣夾住：上游偶爾會吐出離譜的長度，而這個值會直接變成 sleep 的秒數。
    return min(total, 7 * 86400.0)


# 用量／配額（要等到額度回復）。刻意不含 "overloaded"、"server error" ——那些是
# 下面的暫時性故障，兩者的等待策略不同。
_CODEX_USAGE_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "quota",
    "insufficient_quota",
    "you've hit your",
    "weekly limit",
    "5h limit",
)

# 伺服器側暫時性故障（退避重試就好，不必等額度）。
_CODEX_TRANSIENT_MARKERS = (
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporarily unavailable",
    "500",
    "502",
    "503",
    "504",
    "529",
)


def _dorossi_codex_usage_limit(text: str, session_id: str | None = None
                               ) -> "_DorossiUsageLimitError | None":
    """codex 的輸出像不像「方案／配額用量上限」；是就回填好的例外，否則 None。

    `reset_at` 只在能從**相對**寫法算出來時才給值（見
    `_dorossi_extract_retry_after_seconds`）；給不出來就留 None，讓迴圈走
    「短等待起跳、指數退避」的探測——與 Claude 那側同一套策略。

    **Claude 那一側 2026-09-19 的誤判（成功的答案因為討論到 rate limit 被丟掉）在這裡
    不會發生，理由是結構性的**：這支只從 `_codex_stream_verdict` 的 rc != 0 分支被
    呼叫（rc == 0 一律直接 "ok"），而餵進來的文字是 stderr ＋ 失敗事件，
    `agent_message` 的答案文字從來不在裡面（`_CodexStreamState.feed`）。哪天要在
    rc == 0 也檢查，必須先套上 `_dorossi_cc_limit_text_counts` 那條規則。
    """
    if not text:
        return None
    low = str(text).lower()
    if not any(marker in low for marker in _CODEX_USAGE_MARKERS):
        return None
    delta = _dorossi_extract_retry_after_seconds(text)
    reset_at = (time.time() + delta) if delta else None
    return _DorossiUsageLimitError(
        str(text)[:400],
        _dorossi_sanitize_reset_hint(str(text)),
        reset_at=reset_at,
        session_id=session_id or None)


def _dorossi_codex_transient(text: str, session_id: str | None = None
                             ) -> "_DorossiTransientError | None":
    """codex 的輸出像不像「伺服器暫時忙碌」。**必須排在用量上限判定之後**——理由
    與 Claude 那側相同：用量上限有重設時間可以等，比指數退避精準得多。"""
    if not text:
        return None
    low = str(text).lower()
    if any(marker in low for marker in _CODEX_USAGE_MARKERS):
        return None          # 用量上限優先，不在這裡攔
    if not any(marker in low for marker in _CODEX_TRANSIENT_MARKERS):
        return None
    return _DorossiTransientError(str(text)[:400], session_id=session_id or None)


# ---- 「這個錯誤重試有沒有機會成功」 ----------------------------------------
#
# 2026-09-05 補。自走迴圈原本只有三種會續跑的錯誤（用量上限、暫時性故障、以及
# 2026-09-03 才加的那條），其餘**任何**例外都是「貼一句錯誤、`return`、整個無人
# 值守任務結束」。對一個沒人看著的長任務來說，那代表任何一次偶發失敗——後端行程
# 被系統殺掉、一次網路抖動、一個沒預期到的例外——都會讓它整夜停在那裡。
#
# 判準與 `_supervisor.child_exit_is_fatal` 一樣是**「重試會不會有機會成功」**，
# 不是「錯誤嚴不嚴重」。設定寫錯與憑證過期重試也不會成功，但它們的例外型別跟
# 偶發失敗分不出來，只能靠重試上限兜著。
_FATAL_ERROR_TYPES = (
    FileNotFoundError,      # 後端 CLI 不在 PATH 上——重試一百次還是不在
    NotADirectoryError,
    PermissionError,        # 權限問題不會自己好
    ImportError,
    _DorossiCliOptionError,  # CLI 不認得我們傳的旗標——它不會在重試之間自己變新
    _DorossiAuthError,       # CLI 沒有可用的登入——要有人在主機上登入，重試不會自己好
)

# 這些字樣代表「設定／環境本身錯了」，同樣重試無用。
#
# **CLI 沒登入的兩句真實文字刻意不加進來**（「Not logged in · Please run /login」
# 「Failed to authenticate. API Error: 401 …」，2026-09-19 實測）：那一類改由
# `_dorossi_cc_auth_failure` 從結構化欄位判、文字只當退路而且有長度上限，丟的是型別化
# 的 `_DorossiAuthError`（上面那張型別表）。在這裡加字樣等於對**任何**例外訊息做不設
# 上限的子字串比對——而 `RuntimeError("claude -p exited …: result=<答案>")` 的訊息裡
# 裝的是 result 文字，一篇剛好談到 /login 的長答案就會把自走迴圈判死。本月用量上限的
# 判定已經在同一種寫法上摔過兩次（`_DOROSSI_USAGE_NOTICE_MAX_CHARS` 上方）。
_FATAL_ERROR_MARKERS = (
    "not found on path",
    "cli not found",
    "no such file or directory",
    "not authenticated",
    "invalid api key",
    "authentication",
    "permission denied",
)


# ---- 連線探測：網路回來了沒 -------------------------------------------------
#
# 斷網時的等待不是猜一段秒數，而是**問網路**：對該後端（或對話平台）的主機做一次
# DNS 解析 ＋ TCP 連線，成功就算回來了。主機名只給這支用，永遠不會送進對話平台。
DOROSSI_NETWORK_PROBE_HOSTS = {
    "claude_code": ("api.anthropic.com", 443),
    "api": ("api.anthropic.com", 443),
    "codex": ("chatgpt.com", 443),
    "platform": ("discord.com", 443),
}
DOROSSI_NETWORK_PROBE_TIMEOUT_SEC = 8.0


async def dorossi_network_reachable(target: str) -> bool:
    """`target`（`DOROSSI_NETWORK_PROBE_HOSTS` 的鍵）現在連得上嗎？永不 raise。

    只做「解析 ＋ 連上就關」，不送任何資料；整段有上限，卡住的解析器不會把等待
    本身卡死。認不得的 target 退回平台那一台。"""
    host, port = DOROSSI_NETWORK_PROBE_HOSTS.get(
        target, DOROSSI_NETWORK_PROBE_HOSTS["platform"])
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), DOROSSI_NETWORK_PROBE_TIMEOUT_SEC)
    except (OSError, asyncio.TimeoutError, TimeoutError, ValueError):
        return False
    except Exception:  # pylint: disable=broad-except
        return False
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2.0)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    return True


def dorossi_error_is_fatal(exc: BaseException) -> bool:
    """True ＝ 重試沒有意義，該停下來讓人處理。永不 raise。

    保守的方向刻意選「可重試」：判錯成致命 → 無人值守任務白停一整夜；判錯成可
    重試 → 最多多試幾次然後照樣停下來（重試次數有上限）。兩種錯誤的代價差很多。
    """
    try:
        if isinstance(exc, _FATAL_ERROR_TYPES):
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in _FATAL_ERROR_MARKERS)
    except Exception:  # pylint: disable=broad-except
        return False


def dorossi_error_retry_wait_seconds(attempt: int, *, base: float = 20.0,
                                     cap: float = 300.0) -> float:
    """非預期錯誤的第 `attempt` 次（從 1 起算）重試要等幾秒。指數退避、封頂 5 分。

    比暫時性故障那條短（那條起步 30 秒、封頂 15 分）：伺服器過載要給對方時間恢復，
    而這裡多半是本機的偶發失敗，等太久只是浪費無人值守的時間。
    """
    try:
        n = max(1, int(attempt))
    except (TypeError, ValueError):
        n = 1
    return float(min(cap, base * (2 ** (n - 1))))


def dorossi_abandoned_loops(state: dict, *, now: float | None = None) -> list:
    """被中斷、而且**不會再被自動接續**的自走任務。回 `[(uid, sid, age_sec), …]`。

    2026-09-05 補。`_dorossi_loop_autoresume_plan` 有四道條件，任何一道不過就回
    None——標記過舊、重試次數用完、少了頻道錨點、或不是 live。前三種情況下標記
    仍然停在 `live: True`，但**沒有任何東西會再去接它**，也沒有任何地方會講。
    對一個無人值守的長任務來說，那等於「它其實早就停了，而你以為還在跑」。

    這裡只回報「該被接回去、但接不回來」的那些（`live` 還是真的，或因為網路／被取消
    而停下來的，見 `_dorossi_loop_marker_wants_autoresume`）；擁有者 abort 或其他自願
    停下來的任務不算異常，不列入。
    """
    out = []
    if not isinstance(state, dict):
        return out
    for uid, rec in state.items():
        if not isinstance(rec, dict):
            continue
        for sid, sess in (rec.get("sessions") or {}).items():
            if not isinstance(sess, dict):
                continue
            marker = sess.get("loop_pending")
            if not _dorossi_loop_marker_wants_autoresume(marker):
                continue
            if _dorossi_loop_autoresume_plan(sess, now=now) is not None:
                continue          # 還接得回來，不算被遺棄
            ts = marker.get("ts")
            age = ((now if now is not None else time.time()) - float(ts)
                   if isinstance(ts, (int, float)) and not isinstance(ts, bool)
                   else 0.0)
            out.append((str(uid), str(sid), max(0.0, age)))
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def _dorossi_usage_wait_seconds(exc: Exception, attempt: int,
                                *, now: float | None = None) -> float:
    """自走迴圈撞上方案用量上限後，這一次該睡多久（秒）再續跑。

    `attempt` 由 1 起算，是「這一段**連續**等待裡的第幾次」——中間只要有任何一輪
    成功，呼叫端就會歸零。

    兩條路：

    1. `exc.reset_at` 有值（後端給了 `…|<epoch>` 或 API 的 `retry-after`）
       → 睡到那個時刻再加 `DOROSSI_USAGE_WAIT_GRACE_SEC` 緩衝。
    2. 沒有值 → 從 `DOROSSI_USAGE_WAIT_FALLBACK_SEC` 起跳、每次加倍的**退避探測**。
       不去猜「resets 3:45pm」那種沒有時區的鐘點（見 `_dorossi_extract_reset_epoch`）。

    兩條路的結果都 clamp 進 `[DOROSSI_USAGE_WAIT_MIN_SEC,
    DOROSSI_USAGE_WAIT_MAX_SEC]`：下限擋誤判成用量上限時的熱迴圈，上限確保就算後端
    報了一個荒謬的未來時刻，也最多睡 max 就再探一次。

    純函式（`now` 可注入），永不 raise。
    """
    now = time.time() if now is None else now
    reset_at = getattr(exc, "reset_at", None)
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        remain = float(reset_at) - float(now) + DOROSSI_USAGE_WAIT_GRACE_SEC
        # 時間戳已經過去（時鐘偏移／後端報了舊視窗）→ 不是「不用等」，而是
        # 「這個時間戳沒有參考價值」，退回退避探測，不要立刻重試。
        if remain > 0:
            return _dorossi_clamp_usage_wait(remain)
    try:
        shift = min(max(0, int(attempt) - 1), DOROSSI_USAGE_WAIT_MAX_SHIFT)
    except (TypeError, ValueError):
        # 「永不 raise」是這支的合約，而它整條路徑都在「已經出事了」的處理流程上；
        # attempt 傳成怪東西時退回第一次的等待長度，不要把用量上限處理本身炸掉。
        shift = 0
    return _dorossi_clamp_usage_wait(
        DOROSSI_USAGE_WAIT_FALLBACK_SEC * (2 ** shift))


def _dorossi_clamp_usage_wait(secs: float) -> float:
    """把等待秒數收進 `[MIN, MAX]`。上限本身也 clamp 到不小於下限，免得有人在
    設定檔裡把 max 設成 10 秒，反而把空轉防護關掉。

    **`nan` 必須用一道明確的閘擋掉，靠 `min`／`max` 是擋不住的。** nan 的所有比較
    都回 False，而 CPython 的 `max(a, b)` 是「先取 a，再看 `b > a`」——所以結果
    完全取決於引數順序：`max(MIN, nan)` 回 MIN（nan 被丟掉），`max(nan, MIN)` 回
    nan（一路傳下去）。這裡本來寫的正是後者，於是 2026-09-08 實測 nan 進、nan
    出，夾擠形同不存在。同一個形狀已經記在 `_gui_control.parse_duration` 與
    `_batch_config._is_finite_number`，這是第三次——所以寫成明確的閘而不是靠引數
    順序：順序的正確性是隱形的，下一個人重排它不會有任何症狀。

    刻意**不**寫成 `isfinite`：`inf` 與 `-inf` 現在的行為是對的且有意義——`inf`
    夾到 ceiling（「就算後端報了一個荒謬的未來時刻，也最多睡 max 就再探一次」正是
    上限的用途），`-inf` 夾到 MIN。只有 nan 是「沒有任何資訊」，沒有一個有意義的
    夾擠結果，所以單獨處理。

    nan 回 **MIN** 而不是 MAX：這支的下限是「誤判成用量上限時的熱迴圈防護」，回
    MIN 等於「等最短的那一段再探一次」；回 MAX 會讓一個無意義的數字把無人值守的
    自走迴圈停掉 6 小時，那是比較貴的錯誤方向。

    目前 nan 進不來（上游 `_dorossi_usage_wait_seconds` 的 `if remain > 0` 對 nan
    是 False，會落到退避那條路）——但那是**巧合的保護**：那個判斷不是為 nan 而寫
    的，而 `json.loads` 預設就吃 `NaN`，所以來源真的給得出 nan。合約在自己這裡守住。
    """
    value = float(secs)          # 先轉換再判 nan，保住原本對數值字串／Decimal 的接受度
    if math.isnan(value):
        return DOROSSI_USAGE_WAIT_MIN_SEC
    ceiling = max(float(DOROSSI_USAGE_WAIT_MAX_SEC), DOROSSI_USAGE_WAIT_MIN_SEC)
    return min(max(value, DOROSSI_USAGE_WAIT_MIN_SEC), ceiling)


def _dorossi_cc_budget_exceeded(result_ev: dict) -> bool:
    """True when a claude_code `result` event indicates our per-invocation
    --max-budget-usd cap was hit. The CLI signals this with
    subtype == "error_max_budget_usd" (and an `errors` entry like "Reached
    maximum budget ($X)"); the run exits non-zero with NO `result` answer. This
    is OUR own spend cap, NOT a stale session and NOT a plan/quota usage limit,
    so the caller must treat it gracefully (no resume retry, no exception)."""
    if not isinstance(result_ev, dict):
        return False
    if result_ev.get("subtype") == "error_max_budget_usd":
        return True
    errs = result_ev.get("errors")
    if isinstance(errs, list):
        return any("maximum budget" in str(e).lower() for e in errs)
    return False


def _dorossi_count(value) -> int | None:
    """一個 token 計數欄位 → 非負整數；不是可用的數字就回 None。永不 raise。

    `json.loads` 預設收 `NaN`／`Infinity`，而 `int(float("nan"))` 丟 ValueError、
    `int(float("inf"))` 丟 OverflowError——這三支用量解析都在「答案已經拿到」之後才跑
    （`_dorossi_via_claude_code` 的 return 那一行），在那裡丟例外等於把一個答完的回合
    打成失敗。bool 是 int 的子類別，也要排除。負數當成壞值（token 數不會是負的）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _dorossi_usage_int(usage: dict, keys) -> int:
    """Sum the named integer fields out of a claude_code `result.usage` block,
    defensively: missing / non-numeric / bool / non-finite / negative values
    contribute 0. Returns a plain int (>= 0). Never raises."""
    total = 0
    if not isinstance(usage, dict):
        return 0
    for k in keys:
        v = _dorossi_count(usage.get(k))
        if v is not None:
            total += v
    return total


def _dorossi_model_usage_totals(result_ev: dict) -> dict | None:
    """把 `result.modelUsage` 的每模型用量加總成 `{"in","cr","cc","out"}`。

    為什麼需要它：頂層 `usage` **只算主模型**，但 `total_cost_usd` 是**所有**模型
    的總和。2026-08-30 用真的 CLI 量到一次一般的問答輪——頂層 `usage.input_tokens`
    是 2，而 `modelUsage` 裡除了主模型之外還有一個小模型吃掉 897 input / 9 output、
    花掉 $0.000942，`total_cost_usd` 把兩者都算進去了。也就是說只讀頂層 `usage` 的
    話，token 與金額本來就對不起來，而這份紀錄存在的理由正是拿來比對兩者。

    鍵名是 camelCase（`inputTokens` / `cacheReadInputTokens` /
    `cacheCreationInputTokens` / `outputTokens`），與頂層 `usage` 的 snake_case
    不同——所以不能共用 `_dorossi_usage_int`。整塊缺席／不是 dict 就回 None，讓
    呼叫端退回頂層 `usage`。永不 raise。
    """
    models = result_ev.get("modelUsage") if isinstance(result_ev, dict) else None
    if not isinstance(models, dict) or not models:
        return None
    totals = {"in": 0, "cr": 0, "cc": 0, "out": 0}
    keys = {"in": "inputTokens", "cr": "cacheReadInputTokens",
            "cc": "cacheCreationInputTokens", "out": "outputTokens"}
    seen = False
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        seen = True
        for slot, key in keys.items():
            value = _dorossi_count(entry.get(key))
            if value is not None:
                totals[slot] += value
    return totals if seen else None


_CONTEXT_KEYS = ("input_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")


def _dorossi_last_call_context(result_ev) -> int | None:
    """這次叫用**最後一次** API 呼叫的脈絡大小（in＋cache_read＋cache_creation）。

    來源是 `result.usage.iterations` 的最後一筆。2026-09-19 在 CLI 2.1.276 與 2.1.277
    各量一次（一次叫用裡 4 次 API 呼叫）：頂層 `usage` 是**這次叫用所有呼叫的加總**
    （cr 22,904≒4×7.8k），`modelUsage` 再把其他模型也加進去，而 `iterations` **只有
    一筆＝最後一次呼叫**（8＋7,802＋137＝7,947）。壓縮觸發要的是「下一輪 resume 要重送
    多大的前綴」，那就是最後一次呼叫看到的脈絡；加總會把一輪 20 次工具呼叫的前綴算 20
    次（帳本量到 250 萬～1 億，永遠過 300k 門檻 → 每一個工作輪後面都跟一個壓縮輪）。

    形狀不對（沒有 `iterations`、不是 list、最後一筆不是 dict、那一筆一個可用的數字
    都沒有）一律回 None，讓呼叫端退回舊的加總估計——那個方向是「壓得太早」，有界而且
    不會漏壓。`type` 不是 `"message"` 的項目（目前沒見過）跳過、往前找最後一次真的
    模型呼叫。永不 raise。"""
    usage = result_ev.get("usage") if isinstance(result_ev, dict) else None
    iterations = usage.get("iterations") if isinstance(usage, dict) else None
    if not isinstance(iterations, list):
        return None
    for entry in reversed(iterations):
        if not isinstance(entry, dict):
            return None
        kind = entry.get("type")
        if kind is not None and kind != "message":
            continue
        if all(_dorossi_count(entry.get(k)) is None for k in _CONTEXT_KEYS):
            return None
        return _dorossi_usage_int(entry, _CONTEXT_KEYS)
    return None


_CLI_COMMAND_RE = re.compile(r"^/([A-Za-z][A-Za-z0-9_-]{0,31})(?:\s|$)")


def _dorossi_cli_command_of(prompt) -> str:
    """這一輪送出去的是 CLI 的斜線指令（`/compact` 之類）而不是給模型的話嗎？
    是的話回指令名（小寫、不含 `/`），不是的話回空字串。永不 raise。

    用途是**把維護輪標進帳本**（`dorossi_usage.ndjson` 的 `k` 欄位），讓「這筆花費是
    在做壓縮維護、不是在做事」看得出來，不必靠時間相關性去推。實測 2026-08-30：那段
    期間壓縮佔總花費的 9.9%（$39.73／$401.15），而在標記出現之前那些列跟一般工作輪
    長得一模一樣。

    **這不是拿來讓診斷閉嘴的。** 那條「有花費卻讀不到 token」的診斷照樣對維護輪生效
    ——因為實測顯示維護輪的數字是**讀得到**的（見 `_dorossi_cc_round_info` 的說明），
    所以讀不到就真的是回歸。
    """
    if not isinstance(prompt, str):
        return ""
    match = _CLI_COMMAND_RE.match(prompt.lstrip())
    return match.group(1).lower() if match else ""


def _dorossi_cc_round_info(result_ev: dict, *, stderr_tail: str = "",
                           cli_command: str = "") -> dict:
    """Per-invocation usage/cost summary parsed from a claude_code `result`
    event, returned as the 3rd element of `_dorossi_via_claude_code` so the
    autonomous loop can accumulate spend for budget-awareness / the periodic-
    compaction trigger. A plain dict keeps it extensible and never raises.
    Empty/missing event → 0.0 cost / 0 tokens.

    Keeps the `cost_usd` key UNCHANGED (B2's compaction trigger depends on it).
    The input token count is SPLIT (not folded) into three diagnostic buckets so
    the owner-only `@bot tokens` chart can tell cheap cache HITS from expensive
    cache REBUILDS (i.e. whether B1/B2's caching actually pays off):
      * `in`  = fresh input tokens
      * `cr`  = cache-read input tokens      (cheap — a cache hit)
      * `cc`  = cache-creation input tokens  (expensive — a cache rebuild)
      * `out` = output tokens
    Each read defensively (missing / non-numeric / bool → 0). These numbers are
    diagnostics / owner-only chart DATA — they NEVER reach Discord.

    **來源優先 `modelUsage`，頂層 `usage` 只是退路**（2026-08-30 改）：`usage` 只算
    主模型，而 `total_cost_usd` 是所有模型的總和，兩者放同一列會對不起來。細節見
    `_dorossi_model_usage_totals`。
    """
    cost = 0.0
    fresh = cread = ccreate = out = 0
    ctx = None
    if isinstance(result_ev, dict):
        raw = result_ev.get("total_cost_usd")
        if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                and math.isfinite(raw) and raw >= 0):
            # 非有限的金額（`json.loads` 收 NaN）會讓 `cost_since_compact` 變成 nan，
            # 而 nan 的所有比較都回 False——花費觸發就此安靜失效。
            cost = float(raw)
        ctx = _dorossi_last_call_context(result_ev)
        totals = _dorossi_model_usage_totals(result_ev)
        if totals is not None:
            fresh, cread = totals["in"], totals["cr"]
            ccreate, out = totals["cc"], totals["out"]
        else:
            usage = result_ev.get("usage")
            if isinstance(usage, dict):
                fresh = _dorossi_usage_int(usage, ("input_tokens",))
                cread = _dorossi_usage_int(usage, ("cache_read_input_tokens",))
                ccreate = _dorossi_usage_int(usage, ("cache_creation_input_tokens",))
                out = _dorossi_usage_int(usage, ("output_tokens",))
        if cost > 0.0 and not (fresh or cread or ccreate or out):
            # `stderr_tail` 只在這一條異常路徑用得到，所以是關鍵字參數、預設空字串
            # ——正常那一輪不需要它，而正常那一輪佔絕大多數。
            # 有花費卻一個 token 都讀不到＝這個 result 事件的形狀跟我們預期的不一樣。
            # 靜默記 0 的話，`dorossi_usage.ndjson` 會多一筆「花了錢、沒用 token」的
            # 資料點，而那份紀錄的用途正是拿 token 對帳金額。
            #
            # **舊帳本裡那 19 筆的成因已經查清楚了（2026-08-30，實跑兩次可重現）：**
            # 那是自走迴圈的 `/compact` 維護輪。`/compact` 那一輪的 result 事件
            # **有** `usage` 這個鍵，但裡面五個數字**全是 0**；真正的數字只出現在
            # `modelUsage`（實測 in=2063、out=1661、cache_read=18115，`costUSD` 與
            # `total_cost_usd` 完全相等）。也就是說先前記為「已排除 /compact——實測
            # usage 完整」的那個判斷只看了鍵在不在、沒看值。
            # 而本檔今天改成**優先讀 `modelUsage`** 之後，這一類就讀得到了——實測把
            # 真實事件餵進 `_dorossi_cc_round_info` 會得到那四個非零數字。舊資料還在
            # 是因為線上的 bot 是 2026-08-26 起的行程，還沒載到這段程式。
            # **所以走到這裡就是回歸**：連 `modelUsage` 都讀不到，帳本要對不起來。
            # 只印鍵名與 subtype，不印值（這裡什麼都可能有），而且只進 stderr／log，
            # 不會到 Discord。
            tail = (f" stderr tail: {stderr_tail[-300:]!r}"
                    if stderr_tail else "")
            print(f"[dorossi] result event has cost {cost:.4f} but no usage: "
                  f"subtype={result_ev.get('subtype')!r} "
                  f"is_error={result_ev.get('is_error')!r} "
                  f"keys={sorted(result_ev)}{tail}", file=sys.stderr)
    info = {"cost_usd": cost, "in": fresh, "cr": cread,
            "cc": ccreate, "out": out}
    if ctx is not None:
        # 最後一次 API 呼叫的脈絡大小（見 `_dorossi_last_call_context`）。只在讀得到時才
        # 放，缺席＝「退回加總估計」，由 `_dorossi_context_tokens` 處理。
        info["ctx"] = ctx
    if cli_command:
        # 記進帳本，這樣「有花費、0 token」那些列自己就說得出原因，不必再靠推理。
        info["kind"] = cli_command
    return info


# ---------------------------------------------------------------------------
# 每次叫用的金額／token：CLI 在 2.1.277 把 `--resume` 的總額改成工作階段累計
#
# 2.1.277（2026-09-18）變更記錄：「Fixed a headless resume (`claude -p --resume`, …)
# starting the session's cost and usage totals at zero; headless sessions now save their
# totals at exit」。2026-09-19 用隔離的 2.1.277 與本機 2.1.276 各在同一個工作階段連叫
# 三次實測：2.1.276 的 `total_cost_usd` 0.0141 → 0.0010 → 0.0010（每次叫用）；2.1.277
# 0.0134 → 0.0144 → 0.0154（**工作階段累計**），`modelUsage` 的 token 同樣累計；頂層
# `usage` 與 `usage.iterations` 兩版都是**每次叫用**。result 事件裡沒有任何「本次叫用」
# 的金額欄位，官方 SDK 成本文件卻還寫著「each result reflects only that call」——上游
# 語意還在變，所以這裡必須兩種都對，而且不能靠文件。
#
# 為什麼這件事要緊：自走迴圈把每輪的 `cost_usd` 加總成 `cost_since_compact`（花費觸發
# 預設 $10），帳本逐輪記錄。累計值逐輪相加是 O(N²)：一個跑了一陣子的工作階段每一輪都
# 會「超過 $10」→ 每一輪後面都插一個壓縮輪。而 bot 每輪重新起 CLI，**CLI 自動更新一到
# 就中，不必重啟 bot**。
# ---------------------------------------------------------------------------
# 從這一版起 `--resume` 的 `total_cost_usd`／`modelUsage` 是工作階段累計。
_CLAUDE_CUMULATIVE_TOTALS_FROM = (2, 1, 277)
_CLI_VERSION_RE = re.compile(r"^\s*(\d{1,4})\.(\d{1,4})\.(\d{1,6})(?!\d)")
_ACCOUNT_TOKEN_KEYS = ("in", "cr", "cc", "out")


def _dorossi_cc_version_tuple(version) -> tuple | None:
    """`"2.1.277"`（init 事件的 `claude_code_version`）→ `(2, 1, 277)`；讀不懂回 None。"""
    if not isinstance(version, str):
        return None
    match = _CLI_VERSION_RE.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _dorossi_cc_totals_mode(version) -> str | None:
    """這個 CLI 版本 `--resume` 回報的總額是 `"cumulative"` 還是 `"per_call"`；
    版本讀不到回 None（交給 `_dorossi_cc_account_round` 決定怎麼辦）。"""
    parsed = _dorossi_cc_version_tuple(version)
    if parsed is None:
        return None
    return "cumulative" if parsed >= _CLAUDE_CUMULATIVE_TOTALS_FROM else "per_call"


def _dorossi_usage_mark_of(mark) -> dict | None:
    """把存在工作階段槽裡的累計基準（`cc_usage_mark`）驗過一次再用。

    這份資料來自磁碟（`dorossi_session.json`，本機可手改），形狀不對一律當成「沒有
    基準」——那個方向在累計模式下是「這一輪不計金額」，不會膨脹。永不 raise。"""
    if not isinstance(mark, dict):
        return None
    sid = mark.get("sid")
    mode = mark.get("mode")
    cost = mark.get("cost")
    if not isinstance(sid, str) or not sid:
        return None
    if mode not in ("cumulative", "per_call", None):
        return None
    if (isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(cost) or cost < 0):
        return None
    out = {"sid": sid, "mode": mode, "cost": float(cost),
           "tok": mark.get("tok") if mark.get("tok") in ("model", "usage") else None}
    for key in _ACCOUNT_TOKEN_KEYS:
        value = _dorossi_count(mark.get(key))
        if value is None:
            return None
        out[key] = value
    return out


def _dorossi_per_call(current: list, cumulative: bool, base, floor: list):
    """一個計數器的「本次叫用」值：回傳 (值, 標籤)。

    * 不是累計 → 原值（`"call"`）。
    * 累計但沒有基準 → `floor`（`"base"`）：分不出這次叫用佔多少，**寧可少算也不膨脹**
      ——把整個工作階段的總額算成一輪，正是要修的那個缺陷。
    * 差值有負數，或差值總和明顯小於 `floor` 的總和（`floor` 是這次叫用**一定**至少有的
      量）→ 計數器重新起算過（壓縮、工作階段重建……）或基準不屬於這一段，回原值
      （`"reset"`）。「明顯」留了一點容差（5%＋64）：兩份數字來自 CLI 兩個不同的加總，
      差幾個 token 不該把一個正確的差值打成 reset——reset 回的是原值，在累計模式下那是
      會膨脹的方向。
    * 其餘 → 差值（`"delta"`）。
    """
    if not cumulative:
        return list(current), "call"
    if base is None:
        return list(floor), "base"
    delta = [c - b for c, b in zip(current, base)]
    floor_total = sum(floor)
    tolerance = 64 + floor_total * 0.05
    if any(d < 0 for d in delta) or sum(delta) + tolerance < floor_total:
        return list(current), "reset"
    return delta, "delta"


def _dorossi_cc_account_round(info: dict, result_ev, *, resumed_id=None, sid=None,
                              cli_version=None, baseline=None) -> dict:
    """把 `_dorossi_cc_round_info` 的原始數字換成**這次叫用**的數字，並附上下一次要用的
    累計基準（`usage_mark`）。純函式、永不 raise；回傳新的 dict，不改 `info`。

    規則（兩種 CLI 語意都要對）：
    * 沒有 `--resume`（新工作階段）→ 回報值就是這次叫用，兩版都一樣。
    * 版本 < 2.1.277（`per_call`）→ 回報值就是這次叫用。
    * 版本 ≥ 2.1.277（`cumulative`）→ 這次叫用＝目前累計 − 同一個工作階段上一次存下的
      累計（`baseline`）。基準只有在「`sid` 等於這次 resume 的 id、而且當時也是累計
      模式」時才算數：2.1.276 存下的是**每次叫用**的值，拿它當累計基準會把整個工作
      階段算成一輪（升版當天每個工作階段都會踩到）。
    * 版本讀不到 → 沿用同一個工作階段上一次的模式（`baseline["mode"]`）；連那個都沒有
      就當 `per_call`，也就是改動前的行為。
    * 累計模式沒有可用的基準（升版後第一次 resume、槽裡的基準遺失）→ 金額記 0、token
      記頂層 `usage`（**兩版都是每次叫用、只算主模型**，是這次叫用一定至少有的量）。
      這是刻意的取捨：分不出來的時候少算一輪，而不是把整個工作階段算成一輪；存下的
      基準讓下一輪起就精確。標籤 `"base"` 會進帳本，事後查得到。
    * 差值是負的（計數器重新起算）或比頂層 `usage` 還小（基準不屬於這一段）→ 視為從
      零起算，用回報值（標籤 `"reset"`）。

    token 與金額是兩個計數器：token 只有在來自 `modelUsage`（累計那一份）時才會累計；
    `modelUsage` 缺席時退回的頂層 `usage` 本來就是每次叫用。兩者共用同一個基準，所以
    任何一個判成 reset，另一個也一起 reset。

    `usage_mark` 記的一律是**原始回報值**（不是換算後的），因為下一輪要拿它跟原始回報值
    相減。它只在知道後端的工作階段 id 時才有。
    """
    out = dict(info) if isinstance(info, dict) else {}
    raw_cost = out.get("cost_usd")
    if (isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float))
            or not math.isfinite(raw_cost) or raw_cost < 0):
        raw_cost = 0.0
    raw_cost = float(raw_cost)
    raw_tokens = [_dorossi_count(out.get(k)) or 0 for k in _ACCOUNT_TOKEN_KEYS]
    usage = result_ev.get("usage") if isinstance(result_ev, dict) else None
    floor_tokens = [_dorossi_usage_int(usage, (key,)) for key in (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens")]
    token_source = ("model" if _dorossi_model_usage_totals(result_ev) is not None
                    else "usage")

    base = _dorossi_usage_mark_of(baseline)
    if base is not None and (not resumed_id or base["sid"] != resumed_id):
        base = None
    mode = _dorossi_cc_totals_mode(cli_version)
    if mode is None and base is not None:
        mode = base["mode"]
    cumulative = bool(resumed_id) and mode == "cumulative"
    cum_base = base if base is not None and base["mode"] == "cumulative" else None

    cost_vals, cost_label = _dorossi_per_call(
        [raw_cost], cumulative,
        None if cum_base is None else [cum_base["cost"]], [0.0])
    token_cumulative = cumulative and token_source == "model"
    token_base = (None if cum_base is None or cum_base["tok"] != "model"
                  else [cum_base[k] for k in _ACCOUNT_TOKEN_KEYS])
    token_vals, token_label = _dorossi_per_call(
        raw_tokens, token_cumulative, token_base, floor_tokens)
    # 兩個計數器共用同一個基準：任何一個判定「基準不屬於這一段」，另一個的差值也不可信
    # （同一個基準怎麼可能對金額是對的、對 token 是錯的），兩個一起從原值起算。
    if "reset" in (cost_label, token_label):
        if cost_label == "delta":
            cost_vals, cost_label = [raw_cost], "reset"
        if token_label == "delta":
            token_vals, token_label = list(raw_tokens), "reset"

    out["cost_usd"] = round(max(0.0, cost_vals[0]), 10)
    for key, value in zip(_ACCOUNT_TOKEN_KEYS, token_vals):
        out[key] = int(value)
    out["acct"] = (cost_label if cost_label == token_label
                   else f"{cost_label}/{token_label}")
    if isinstance(cli_version, str) and cli_version:
        out["v"] = cli_version[:32]
    if isinstance(sid, str) and sid:
        mark = {"sid": sid, "mode": mode, "cost": raw_cost, "tok": token_source}
        mark.update(zip(_ACCOUNT_TOKEN_KEYS, raw_tokens))
        out["usage_mark"] = mark
    return out


def _dorossi_trim_usage_file() -> None:
    """Keep DOROSSI_USAGE_FILE bounded: once it grows past _DOROSSI_USAGE_TRIM_AT
    lines, atomically rewrite it to the last _DOROSSI_USAGE_MAX_LINES (hysteresis
    avoids rewriting on every append once near the cap). Never raises."""
    tmp = DOROSSI_USAGE_FILE.with_name(DOROSSI_USAGE_FILE.name + ".tmp")
    try:
        if not DOROSSI_USAGE_FILE.exists():
            return
        with open(DOROSSI_USAGE_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= _DOROSSI_USAGE_TRIM_AT:
            return
        keep = lines[-_DOROSSI_USAGE_MAX_LINES:]
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, DOROSSI_USAGE_FILE)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage trim failed: {exc!r}", file=sys.stderr)
        # `os.replace` **只有成功時**才把 temp 搬走。這支的合約是絕不往外拋，所以
        # 不重拋，但也不能把半份資料留在 repo root：留著的話下一次修剪會直接覆寫
        # 它（無害），可是它會一直躺在那裡看起來像真的資料，而且它是
        # `test_gitignore_coverage.py` 盯的那種「repo root 執行期產物」。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _dorossi_record_usage(info: dict) -> None:
    """Append ONE token-usage data point (built from a round-info dict) to
    DOROSSI_USAGE_FILE, then trim. Writes the SPLIT schema
    `{"ts","in","cr","cc","out","cost_usd"}` (in=fresh / cr=cache_read /
    cc=cache_creation / out=output). Fail-soft: any error is logged to stderr
    and swallowed — recording usage must NEVER break a Dorossi turn nor surface
    to Discord. Missing fields are recorded as 0."""
    info = info if isinstance(info, dict) else {}

    def _i(key):
        v = info.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    try:
        rec = {
            "ts": time.time(),
            "in": _i("in"),
            "cr": _i("cr"),
            "cc": _i("cc"),
            "out": _i("out"),
            "cost_usd": (float(info.get("cost_usd") or 0.0)
                         if isinstance(info.get("cost_usd"), (int, float))
                         and not isinstance(info.get("cost_usd"), bool) else 0.0),
        }
        kind = info.get("kind")
        if isinstance(kind, str) and kind:
            # 維護輪（CLI 斜線指令）的標記。只在非空時寫，一般工作輪的列維持原樣，
            # 舊資料與舊讀取端都不受影響。長度已由 `_CLI_COMMAND_RE` 限死在 32 字元。
            rec["k"] = kind[:32]
        # 以下三欄都是 2026-09-19 加的，只在有值時寫，舊列與舊讀取端不受影響：
        # * `ctx`  最後一次 API 呼叫的脈絡大小（壓縮觸發用的就是它）。有了它，事後才對得出
        #          「這一輪為什麼（沒）壓縮」，in/cr/cc 是整次叫用的加總、對不出來。
        # * `acct` 金額／token 是怎麼換算成「這次叫用」的（見 `_dorossi_cc_account_round`）。
        #          `"call"` 不寫（就是回報值本身，跟舊列同義）。
        # * `v`    CLI 版本。帳本會橫跨 2.1.277 的語意變更，沒有這欄就分不出哪幾列是換算過的。
        ctx = _dorossi_count(info.get("ctx"))
        if ctx is not None:
            rec["ctx"] = ctx
        acct = info.get("acct")
        if isinstance(acct, str) and acct and acct != "call":
            rec["acct"] = acct[:16]
        version = info.get("v")
        if isinstance(version, str) and version:
            rec["v"] = version[:32]
        with open(DOROSSI_USAGE_FILE, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        _dorossi_trim_usage_file()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage record failed: {exc!r}", file=sys.stderr)


def _dorossi_round_info_and_record(result_ev: dict, *,
                                   stderr_tail: str = "",
                                   cli_command: str = "",
                                   resumed_id: str | None = None,
                                   sid: str | None = None,
                                   cli_version: str | None = None,
                                   baseline: dict | None = None) -> dict:
    """Build the per-invocation round-info dict AND append it to the usage log.
    Used at every `_dorossi_via_claude_code` return so single-turn and every
    autonomous-loop round each contribute one data point.

    回傳的是**這次叫用**的數字（`_dorossi_cc_account_round` 換算過），不是 CLI 的原始
    回報值：2.1.277 起 `--resume` 回報的是工作階段累計，直接拿去加總（`cost_since_compact`）
    或記帳都會重複計算。`resumed_id`／`sid`／`cli_version`／`baseline` 就是換算要的四樣
    東西（這次 resume 的 id、後端回報的 id、init 事件的版本、槽裡存的上一次累計）；全部
    省略時＝新工作階段，行為與改動前一致。回傳值多帶一個 `usage_mark`，呼叫端要把它
    存回同一個槽，下一輪才有基準。

    `stderr_tail` 只是往下傳給「有花費卻讀不到 token」那條診斷；所有失敗路徑早就會印
    stderr 尾巴，只有成功路徑把它丟掉。那條診斷看的是**原始**回報值。

    `cli_command` 標出「這一輪送的是 CLI 的斜線指令而不是給模型的話」，寫進帳本的
    `k` 欄位，讓維護輪的花費跟工作輪分得開（實測那段期間壓縮佔 9.9%）。它**不影響**
    上面那條診斷——維護輪的數字讀得到，讀不到就是回歸。
    """
    info = _dorossi_cc_round_info(result_ev, stderr_tail=stderr_tail,
                                  cli_command=cli_command)
    info = _dorossi_cc_account_round(info, result_ev, resumed_id=resumed_id, sid=sid,
                                     cli_version=cli_version, baseline=baseline)
    _dorossi_record_usage(info)
    return info


def _dorossi_read_usage(limit: int) -> list[dict]:
    """Return up to the last `limit` usage data points (oldest → newest) parsed
    from DOROSSI_USAGE_FILE. Never raises: a missing / unreadable file yields [],
    and malformed lines are skipped. Backend DATA for the owner-only chart."""
    try:
        if not DOROSSI_USAGE_FILE.exists():
            return []
        with open(DOROSSI_USAGE_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] usage read failed: {exc!r}", file=sys.stderr)
        return []
    records: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = _json.loads(raw)
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records[-limit:] if limit > 0 else records


def _dorossi_context_tokens(info: dict) -> int:
    """這一輪結束時的脈絡大小（token），給兩個壓縮觸發用。永不 raise，回傳 int ≥ 0。

    優先用 `info["ctx"]`＝**最後一次 API 呼叫**的 in＋cache_read＋cache_creation（見
    `_dorossi_last_call_context`）：那就是下一輪 resume 要重送的前綴。

    **2026-09-19 之前這裡是 `in＋cr＋cc`，而那三個數字是整次叫用所有 API 呼叫的加總**
    （`modelUsage` 還再加上其他模型）。full 模式一輪動輒幾十次工具呼叫，同一段前綴被
    算幾十次：帳本量到 250 萬～1 億，永遠過 300k 門檻，**每一個工作輪後面都跟一個壓縮
    輪**（末 40 筆嚴格交替，每次 $0.3～4.5、約 3 分鐘、每輪丟一次細節）。舊 docstring
    寫的「高估只是早一點壓縮」不成立——高估的倍數等於工具呼叫次數。

    `ctx` 缺席（`iterations` 讀不到）才退回那個加總——方向是壓得太早，有界而且不會漏壓。"""
    if isinstance(info, dict):
        ctx = _dorossi_count(info.get("ctx"))
        if ctx is not None:
            return ctx
    return _dorossi_usage_int(info, ("in", "cr", "cc"))


def _dorossi_context_compaction_due(context_tokens: int) -> bool:
    """True when a round's context size (see `_dorossi_context_tokens`) has
    reached the configured token threshold `DOROSSI_COMPACT_CONTEXT_TOKENS`
    (0 = disabled). Shared by BOTH the single-turn chat path and the autonomous
    loop, so both compact on the same context-size boundary. The owner-mandated
    token-reduction lever — no effort/model/tool changes."""
    return (DOROSSI_COMPACT_CONTEXT_TOKENS > 0
            and context_tokens >= DOROSSI_COMPACT_CONTEXT_TOKENS)


def _dorossi_loop_compaction_due(rounds_since_compact: int,
                                 cost_since_compact: float,
                                 context_tokens: int = 0) -> bool:
    """True when the autonomous loop should insert a periodic-compaction
    maintenance round: `rounds_since_compact` has reached the configured round
    interval, OR `cost_since_compact` has crossed the configured dollar threshold,
    OR the last round's `context_tokens` has crossed the configured token
    threshold (`_dorossi_context_compaction_due`, the same key the single-turn
    path uses). Each counter is reset right after a compaction; each threshold is
    independently disabled by setting its config key to 0. `context_tokens`
    defaults to 0 (kept keyword-defaulted so pre-existing callers/tests keep
    working) which, together with the >0 guard, is a no-op for the token
    condition. Bounds the O(N²) growth of the unbounded resumed conversation
    WITHOUT capping rounds."""
    if (DOROSSI_LOOP_COMPACT_EVERY_ROUNDS > 0
            and rounds_since_compact >= DOROSSI_LOOP_COMPACT_EVERY_ROUNDS):
        return True
    if (DOROSSI_LOOP_COMPACT_COST_USD > 0
            and cost_since_compact >= DOROSSI_LOOP_COMPACT_COST_USD):
        return True
    if _dorossi_context_compaction_due(context_tokens):
        return True
    return False


def _dorossi_api_is_usage_limit(exc: Exception, rate_err) -> bool:
    """True when an Anthropic SDK error represents a plan/quota usage limit:
    a 429 RateLimitError, a 402 billing error, or a status/message that names
    a rate/usage limit. Tolerant of the SDK being absent (rate_err == ()).
    Never raises — undecidable reads as False."""
    # 整段包起來的理由跟 `_dorossi_api_transient_error` 一樣（`exc` 來自第三方
    # SDK，`status_code` 可能是會自爆的 property、`__str__` 也可能自爆），但這裡
    # **更必要**：本函式排在那一支的前面，先炸的話那邊的防護根本執行不到，整個
    # `except` 區塊會被換成一個看不懂的新例外、原始錯誤連同 traceback 一起消失。
    #
    # `getattr(exc, "status_code", None)` 的預設值只吃 AttributeError；property
    # 拋出來的其他任何例外照樣往外丟，所以那一行本身不是防護。
    #
    # 判不出來時回 False 是安全的方向：往下交給暫時性判定，再不行才裸 `raise`，
    # 也就是完全退回加這層之前的行為。回 True 反而會讓自走迴圈為了一個沒能確認的
    # 上限睡上好幾個小時。
    try:
        if rate_err and isinstance(exc, rate_err):
            return True
        status = getattr(exc, "status_code", None)
        if status in (429, 402):
            return True
        msg = str(exc).lower()
        return ("rate_limit" in msg or "rate limit" in msg
                or "usage limit" in msg or "billing" in msg)
    except Exception:  # pylint: disable=broad-except
        return False


def _dorossi_api_retry_after_sec(exc: Exception) -> float | None:
    """Read the `retry-after` header (seconds) off an Anthropic SDK error.
    Returns None when the header is missing / unusable. Never raises.

    刻意跟 `_dorossi_api_reset_hint` 拆開：這一支的回傳值**會被拿去算等待秒數**
    （自走迴圈睡到額度回來），那一支只是給人看的字串。合成一支就得在「秒數」與
    「格式化字串」之間二選一，兩邊都會將就。"""
    try:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        retry_after = headers.get("retry-after")
        if not retry_after:
            return None
        secs = float(retry_after)
        # NaN／inf 不能靠 `<= 0` 擋掉（`nan <= 0` 是 False，`inf` 更是直接放行），
        # 而 float("nan") / float("inf") 都是合法的 float() 輸入——標頭是外部
        # 來的字串，一律當不可信處理。
        if not (secs > 0) or secs != secs or secs == float("inf"):
            return None
        return secs
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_api_reset_hint(exc: Exception) -> str | None:
    """Read the `retry-after` header off an Anthropic SDK error and render it as
    a wall-clock reset time. Returns None when the header is missing / unusable.
    Never raises."""
    secs = _dorossi_api_retry_after_sec(exc)
    if secs is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.time() + int(secs)))
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_api_transient_error(exc: Exception
                                ) -> "_DorossiTransientError | None":
    """SDK 例外像不像「伺服器暫時性故障」；是就回一個填好的例外，否則 None。

    與 `_dorossi_cc_transient_error` / `_dorossi_codex_transient` 同一個形狀，
    **而且必須排在用量上限判定的後面**：429 也是「等一下再來」，但它有專屬的等待
    策略（讀 `retry-after`／等到額度重設），比這裡的指數退避精準得多。

    2026-09-05 補。在這之前 api 這條路**完全沒有**這個判定：429／402 之外的一切
    （含 529 Overloaded 與 5xx）都是裸 `raise`，落進自走迴圈的泛用 `except`，用
    20s→40s→80s 最多三次的短退避處理——合計不到兩分半，而 2026-09-03 那場過載
    持續了好幾分鐘以上。另外兩條後端早就走專屬的長退避（30s 起、封頂 15 分、
    最多 20 輪），這裡只是把它們補齊。

    判定同時看 `status_code`（SDK 的 `APIStatusError` 家族會帶）與訊息字樣：
    連線層的失敗（`APIConnectionError`）沒有狀態碼，只留下文字。
    """
    # 整段包起來，因為兩個輸入都不是我們控制的：`exc` 來自第三方 SDK，
    # `status_code` 可能是 property（讀取本身就會炸），`__str__` 也可能自爆。
    # 這支是在 `except` 區塊裡被呼叫的——它自己拋例外會把一個「等一下就好」的
    # 伺服器錯誤換成一個看不懂的新例外，而原本的錯誤連同它的 traceback 一起消失。
    # 判不出來時回 None（＝不是暫時性）是安全的方向：呼叫端會落回裸 `raise`，
    # 也就是補這個判定之前的行為。
    try:
        status = getattr(exc, "status_code", None)
        try:
            status_int = int(status) if status not in (None, "") else None
        except (TypeError, ValueError):
            status_int = None
        low = f"{type(exc).__name__}: {exc}".lower()
        hit = (status_int in DOROSSI_TRANSIENT_STATUSES
               or any(marker in low for marker in _TRANSIENT_TEXT_MARKERS))
        if not hit:
            return None
        return _DorossiTransientError(
            (str(exc).strip() or f"status_code={status_int}")[:400],
            status=status_int)
    except Exception:  # pylint: disable=broad-except
        return None


def _dorossi_trim_api_history(history, cap: int | None = None) -> list:
    """把 `api` 後端要重送的歷史修剪成最後 `cap` 則，並切齊到 user 開頭。

    這條路徑只有 `api` 後端會走。API 是無狀態的，所以每一輪都要把整份歷史再送一
    次；不修剪的話輸入 token 隨輪數線性成長（總成本是輪數的平方），而且遲早會超過
    脈絡窗拿到 400。**400 不是暫時性錯誤**，所以自走迴圈會用同一份過長的歷史重試
    到放棄，然後每一輪都以完全相同的方式失敗——沒有任何自我修復的路徑，除非有人
    知道要下 `/new`。滑動視窗會丟掉最早的脈絡，但「記得少一點」遠好過「從此壞掉」。

    切齊 user 是必要的，不是整潔：Messages API 不接受以 assistant 開頭的
    `messages`，而從中間切下去有一半的機率正好切在 assistant 那一則。往後多丟一則
    而不是往前多留一則——多留會超過上限，等於這個界限有時候不成立。

    **切齊是無條件的，修剪才有條件。** 一份沒有超過上限、但本身就以 assistant 開頭
    的歷史（界限上線前存下來的那些就是）照樣會被 API 打回 400，所以不能只在有修剪
    的時候才切。`cap <= 0` ＝不限制（與 `dorossi_max_budget_usd` 同慣例），但即使
    不限制也還是要切齊。

    真的動到東西時寫一行 stderr：這是預期中的行為不是故障，但「答案為什麼忘了前面
    講過的事」總有一天有人要查，而查的時候沒有任何紀錄就等於查不到。沒動就不出聲
    ——每一輪都印一行沒事的訊息，下場是沒人再看它。
    """
    if cap is None:
        cap = DOROSSI_API_HISTORY_MAX_MSGS
    rows = [r for r in (history or []) if isinstance(r, dict)]
    kept = rows if (cap <= 0 or len(rows) <= cap) else rows[-cap:]
    if kept and kept[0].get("role") != "user":
        kept = list(kept)
        while kept and kept[0].get("role") != "user":
            kept.pop(0)
    if len(kept) != len(rows):
        print(f"dorossi api history trimmed: {len(rows)} -> {len(kept)} "
              f"messages (cap {cap})", file=sys.stderr)
    return kept


async def _dorossi_via_api(prompt: str, history: list,
                           model: str | None = None) -> tuple[str, list]:
    """Answer via the Anthropic API (SDK), carrying `history` for continuity.
    Returns (answer, new_history). The API is stateless, so the history is
    resent each turn — bounded to the last `dorossi_api_history_max_msgs`
    messages by `_dorossi_trim_api_history`, because "resend everything" with no
    ceiling ends in a 400 that never recovers. Raises on missing client / API
    error (the SDK defers the credential check to request time).

    `model` 是這一輪 `/model` 解析出來的**完整 model id**（`dorossi_resolve_model`
    的輸出，allowlist 查表命中才會有值）；None ＝這個工作階段沒指定，用
    `DOROSSI_MODEL`。2026-09-23 之前這裡寫死 `DOROSSI_MODEL`，所以 `/model` 在這個
    後端上是**安靜失效**的：指令回「已更新」，送出去的卻永遠是同一個模型。這條路沒有
    CLI 的別名解析，所以裸別名（`opus`）必須在上游就換成具體 id——那是
    `dorossi_resolve_model` 的第三條路（模型目錄 → 內建表同族最新）。"""
    cli = _get_dorossi_client()
    if cli is None:
        raise RuntimeError("anthropic SDK client unavailable")
    # 修剪在**送出之前**，所以界限同時管住這一輪的成本與存回去的那一份
    # （`new_history` 是從 `msgs` 長出來的，最多只會比上限多兩則，下一輪再收回來）。
    msgs = _dorossi_trim_api_history(history) + [
        {"role": "user", "content": prompt}]
    # Map the SDK's 429 (and 402 billing) to the dedicated usage-limit error so
    # the caller can give an actionable plan/quota reply. RateLimitError exposes
    # the `retry-after` header (seconds) as the reset hint when present.
    rate_err = getattr(anthropic, "RateLimitError", ()) if anthropic else ()
    # 本專案自己的外框（見 `DOROSSI_API_TIMEOUT_SEC` 上方的說明）。用 `asyncio.timeout`
    # 而不是 `wait_for`，是為了 `expired()`：它分得出「外框到了」與「SDK 裡面自己丟出
    # 來的某個 TimeoutError」，後者要照舊走下面的分類。
    ceiling = _dorossi_api_call_ceiling_sec()
    bound = asyncio.timeout(ceiling)
    try:
        async with bound:
            resp = await cli.messages.create(
                model=(model or DOROSSI_MODEL),
                max_tokens=DOROSSI_MAX_TOKENS,
                system=DOROSSI_SYSTEM_PROMPT,
                messages=msgs,
            )
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, TimeoutError) and bound.expired():
            # 走既有的泛用失敗路徑：訊息刻意不含「unavailable」「api_key」之類的字——
            # `_dorossi_error_hint` 的 api 分支會把那些字讀成「沒有憑證」。
            print(f"[dorossi] api call exceeded this project's {ceiling:.0f}s "
                  f"ceiling (request timeout x attempts + retry sleeps); giving up",
                  file=sys.stderr)
            raise TimeoutError(
                f"api call exceeded the {ceiling:.0f}s ceiling") from exc
        if _dorossi_api_is_usage_limit(exc, rate_err):
            retry_after = _dorossi_api_retry_after_sec(exc)
            raise _DorossiUsageLimitError(
                str(exc)[:300], _dorossi_api_reset_hint(exc),
                reset_at=(time.time() + retry_after
                          if retry_after is not None else None)) from exc
        # **順序就是規則**：用量上限先判（它等得比較準），剩下的才問「是不是
        # 伺服器暫時性故障」。倒過來的話 429 會被當成過載，用瞎猜的指數退避取代
        # `retry-after` 帶來的精確等待。
        transient_exc = _dorossi_api_transient_error(exc)
        if transient_exc is not None:
            raise transient_exc from exc
        # 連線層的失敗（沒有狀態碼）：等網路回來再用同一份歷史重送。排在用量上限與
        # 暫時性故障之後，理由同 Claude 那側。
        if _dorossi_api_is_offline(exc):
            raise _DorossiOfflineError(
                f"{type(exc).__name__}"[:400], backend="api") from exc
        raise
    answer = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    # Only advance history on a real answer (don't persist a dangling user turn).
    new_history = msgs + [{"role": "assistant", "content": answer}] if answer else history
    return answer, new_history


# ---- 主機休眠：看門狗不要把睡著的時間算進去（2026-09-22） --------------------
#
# 這台機器是 Modern Standby。睡著的時候整個行程停住，醒來之後 `time.monotonic()`
# 跳了一大段，所有看門狗（閒置、輸出沉默、硬上限）同時到期——一輪做到一半的回合在
# 醒來那一瞬間被當成「閒置太久」砍掉，而它其實只是跟著主機一起睡了。
#
# 做法：看門狗不再一次 `wait_for(readline, 整段)`，而是切成 `_DOROSSI_WATCH_SLICE_SEC`
# 的小段等；一小段實際經過的時間比該等的多出 `DOROSSI_SUSPEND_GAP_SEC` 以上，就當成
# 主機睡過，那一段只算它本來該等的長度，多出來的記進 `_DorossiWatchClock.suspended`
# （硬上限也扣掉它）。兩個時鐘取大的：`time.monotonic()` 在這個平台上睡著時會不會走
# 不一定，牆鐘一定會走——兩個都看，哪一種平台都量得到。牆鐘被校時往前撥也會被讀成
# 「睡過」，代價只是那一輪的看門狗寬限了那麼多秒。
DOROSSI_SUSPEND_GAP_SEC = 30.0
_DOROSSI_WATCH_SLICE_SEC = 5.0
# 這個行程到目前為止偵測到的休眠總秒數與次數（任何一個看門狗量到都算）。只給診斷用。
DOROSSI_SUSPEND_SEEN = {"count": 0, "seconds": 0.0}


def dorossi_note_suspend(gap: float) -> None:
    """記一次偵測到的主機休眠（秒）。永不 raise。"""
    try:
        DOROSSI_SUSPEND_SEEN["count"] += 1
        DOROSSI_SUSPEND_SEEN["seconds"] += float(gap)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def dorossi_elapsed_with_gap(mono_start: float, wall_start: float,
                             expected: float) -> tuple:
    """一段「應該等 `expected` 秒」的等待，實際經過多久、其中多少是主機睡著的時間。

    回 `(elapsed, suspended)`：`suspended` > 0 代表這一段比預期多出
    `DOROSSI_SUSPEND_GAP_SEC` 以上（視為休眠），此時 `elapsed` 只算 `expected`。"""
    elapsed = max(time.monotonic() - mono_start, time.time() - wall_start)
    gap = elapsed - expected
    if gap > DOROSSI_SUSPEND_GAP_SEC:
        return float(expected), float(gap)
    return float(elapsed), 0.0


class _DorossiWatchClock:
    """看門狗的時鐘：`time.monotonic()` 扣掉這一輪量到的休眠。"""

    __slots__ = ("suspended",)

    def __init__(self) -> None:
        self.suspended = 0.0

    def now(self) -> float:
        return time.monotonic() - self.suspended


async def _dorossi_readline_watched(stream, timeout: float,
                                    clock: "_DorossiWatchClock") -> bytes:
    """`asyncio.wait_for(stream.readline(), timeout)` 的休眠感知版本。

    同一個 `readline` 在多個小段之間持續等（不重開，資料不會掉）；只有「醒著的時間」
    累計到 `timeout` 才丟 `asyncio.TimeoutError`。休眠的秒數記進 `clock.suspended`
    與 `DOROSSI_SUSPEND_SEEN`。逾時或被取消時把還沒完成的 `readline` 收掉——
    `StreamReader.readline` 被取消不會吃掉緩衝區裡的資料，下一次呼叫照樣讀得到。"""
    task = asyncio.ensure_future(stream.readline())
    try:
        waited = 0.0
        while True:
            step = max(0.0, min(_DOROSSI_WATCH_SLICE_SEC, timeout - waited))
            mono0, wall0 = time.monotonic(), time.time()
            done, _pending = await asyncio.wait({task}, timeout=step)
            if done:
                return task.result()
            elapsed, slept = dorossi_elapsed_with_gap(mono0, wall0, step)
            if slept:
                clock.suspended += slept
                dorossi_note_suspend(slept)
                print(f"[dorossi] host looks suspended for ~{slept:.0f}s; not "
                      "counting it against this round's watchdogs", file=sys.stderr)
            waited += elapsed
            if waited >= timeout:
                raise asyncio.TimeoutError()
    finally:
        if not task.done():
            task.cancel()


async def _read_stream_all(stream) -> bytes:
    """Drain an asyncio stream to EOF, swallowing errors (used for stderr so a
    full pipe can't deadlock the child while we read stdout).

    **讀到 EOF 才回來，而 EOF 不在我們手上**——它要等管線的所有寫端 handle 都關掉。
    所以這個 coroutine 沒有自己的上限，一律要透過 `_dorossi_drain_stderr` 收，
    不要在任何地方直接 `await` 它。
    """
    try:
        return await stream.read()
    except Exception:  # pylint: disable=broad-except
        return b""


# 收尾用的上限。**這不是「等後端做完事」的上限**（那是看門狗的工作，預設 900/10800 秒），
# 是「行程照理說已經結束了，把它收乾淨」的上限，所以短。正常路徑上這個時間根本
# 花不到：stdout EOF 之後 CLI 毫秒級就離開、管線跟著關閉。
_DOROSSI_REAP_TIMEOUT_SEC = 10.0
# 回頭看一眼 `proc.returncode` 的間隔。這不是輪詢式的等待——`wait()` 一完成就會立刻
# 返回（見 `_dorossi_reap_proc` 用的是 `asyncio.wait` 不是 `sleep`），這個值只決定
# 「管線被握著」那條路上多久發現得了 rc。
_DOROSSI_REAP_POLL_SEC = 0.05
# 連 rc 都問不出來時交給判定的哨符。非 0 → 走既有的「非零離開」分類，不必在
# `_claude_stream_verdict` 裡多開一條分支（它的判定順序本身就是規則，不要動）。
_DOROSSI_UNREAPED_RC = -9


async def _dorossi_reap_proc(proc, timeout: float | None = None) -> int | None:
    """把一個**應該已經結束**的子行程收掉，並在有限時間內回報 rc（拿不到回 None）。

    **為什麼不能只寫 `await proc.wait()`**（2026-09-09 在本機實測，CPython 3.14 /
    Windows）：`BaseSubprocessTransport._wait()` 把自己掛在 `_exit_waiters` 上，而那批
    waiter **只有** `_call_connection_lost` 會叫醒，`_try_finish` 又要求
    `all(p.disconnected)`——也就是 **stdout 與 stderr 都要先 EOF**。只要有一個孫行程
    繼承著那兩個管線的寫端，`await proc.wait()` 就**永遠不返回**；不是慢，是無限。
    而孫行程確實留得下來：Windows 的 `proc.kill()` 是 `TerminateProcess`，只帶走直接
    子行程，而 `dorossi_cc_tools="full"` 正是會在主機上起 shell 的模式。

    `proc.returncode` 走的是另一條路：`_process_exited` 在作業系統層的離開被觀察到的
    當下就把它設好，**與管線無關**。實測數字：kill 之後 0.25 秒 returncode 已經是 1，
    而同一時間一個 pending 的 `wait()` 三秒後仍未完成。（順序也很關鍵——`_wait()`
    開頭有 `if self._returncode is not None: return`，所以**已經設定之後**再呼叫
    `wait()` 會立刻回來；卡住的只有「在設定之前就開始等」的那一次。）

    所以這裡**同時**盯兩個訊號，誰先到算誰：`wait()` 完成（管線正常關閉的路徑，
    零額外延遲）與 `returncode` 出現（管線被握著的路徑）。**不要**寫成「先 `wait_for`
    整個 timeout、逾時再看 returncode」——那會在管線被握著時白等滿一個 timeout，而
    實測 0.25 秒就問得到答案了。

    兩個階段：先給它 `timeout` 秒自己好好離開（正常路徑毫秒級就過了），還沒走才 kill，
    再給 `timeout` 秒收屍。所以最壞是 2×timeout，仍然有限。

    離開時 `wait()` 一律 cancel + gather 收掉，否則只是把「永遠卡住」換成「孤兒任務」。

    `timeout=None` → 在**呼叫時**查模組常數（不要寫成預設引數：預設引數在 `def` 當下
    就固定住，之後改模組常數不會生效，測試也就換不掉那個值）。
    """
    if timeout is None:
        timeout = _DOROSSI_REAP_TIMEOUT_SEC
    if proc.returncode is not None:
        return proc.returncode
    wait_task = asyncio.ensure_future(proc.wait())
    try:
        for phase in (0, 1):
            deadline = time.monotonic() + timeout
            while True:
                if proc.returncode is not None:
                    # 行程已經死了，只是管線還被別人握著 → rc 問得到，不必再等。
                    return proc.returncode
                if wait_task.done():
                    try:
                        return wait_task.result()
                    except Exception:  # pylint: disable=broad-except
                        return proc.returncode
                if time.monotonic() >= deadline:
                    break
                # `asyncio.wait` 而不是 `sleep`：`wait()` 一完成就馬上回來（正常路徑
                # 零額外延遲），同時每 poll 秒有機會回頭看一眼 `returncode`。
                await asyncio.wait({wait_task}, timeout=_DOROSSI_REAP_POLL_SEC)
            if phase == 0:
                # 真的還活著。這是「非預期離開路徑」該做的事：不要把一個 `full` 模式的
                # 後端行程留在主機上跑。
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:  # pylint: disable=broad-except  # nosec B110
                    pass
        return proc.returncode
    finally:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)


async def _dorossi_drain_stderr(task, timeout: float | None = None) -> str:
    """收掉 stderr 抽水任務並取回內容；拿不到就**降級成空字串**，不打掉整輪。

    上限的理由同 `_dorossi_reap_proc`：`_read_stream_all` 等的是 EOF，而 EOF 的到來
    掌握在別人手上。逾時之後一定要 cancel + gather——只加上限不收任務，等於把
    「永遠卡住」換成「孤兒任務」。

    stderr 只餵診斷（`failure_reason` 的第三順位來源，前面還有 result 事件與 stdout
    尾巴），所以拿不到就空字串繼續走判定是正確的降級，不是把錯誤吞掉。

    `timeout=None` 的意義同 `_dorossi_reap_proc`：呼叫時才查模組常數。
    """
    if timeout is None:
        timeout = _DOROSSI_REAP_TIMEOUT_SEC
    if task is None:
        return ""
    try:
        raw = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return raw.decode("utf-8", "replace").strip()
    except (asyncio.TimeoutError, TimeoutError):
        return ""
    except Exception:  # pylint: disable=broad-except
        return ""
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def find_codex_executable() -> str | None:
    """Locate Codex even when a long-running supervisor has a stale PATH.

    `CODEX_CLI_PATH` is the explicit operator override.  On Windows the desktop
    installer uses LocalAppData, which is checked after the normal PATH lookup.
    Only existing regular files are returned; no shell wrapper is involved.

    引號的正規化走 `_dorossi_unquote_dir()`，不要在這裡自己再寫一份。
    2026-09-10 之前這行是 `.strip().strip('"')`，兩個問題：它只脫得掉 `"`，而
    `CODEX_CLI_PATH` 是**從 shell 設定的環境變數**，`CODEX_CLI_PATH='…/codex.exe'`
    是完全正常的寫法——單引號留在字串裡，`Path(...).is_file()` 為 False，於是這個
    被本 docstring 稱為 "the explicit operator override" 的東西**安靜地被忽略**，
    退回 PATH 搜尋，而操作者看不到任何差別。第二個問題是它用的正是
    `_dorossi_unquote_dir` 的 docstring 明文警告過的那種「一路刮掉頭尾引號」寫法
    （真的叫 `'foo'` 的路徑會被改成別的路徑）。同模組內已經有正確的那一份，
    這裡曾經是這條判準在本專案的**第三份**私有實作。
    """
    override = _dorossi_unquote_dir(os.environ.get("CODEX_CLI_PATH", ""))
    candidates = [override, _shutil.which("codex")]
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append(str(
                Path(local_app_data) / "Programs" / "OpenAI" / "Codex"
                / "bin" / "codex.exe"))
    for candidate in candidates:
        if candidate:
            try:
                path = Path(candidate).expanduser()
                if path.is_file():
                    return str(path.resolve())
            except OSError:
                continue
    return None


# ==========================================================================
# 每日模型目錄檢查（2026-09-23）
# ==========================================================================
# 兩個 CLI 都**沒有**「列出模型」的子指令（2026-09-23 查過 `claude --help` 與
# `codex exec --help`）。所以發現的辦法只有兩條，按便宜程度排：
#
#   1. **SDK 的模型清單**（`client.models.list()`）——權威、一次拿到整份目錄，但要
#      憑證。本機沒有設任何憑證環境變數（同日量過），所以這條在這台機器上不會跑；
#      留著是因為換一台有憑證的主機就該走它。
#   2. **探測**——拿**裸別名**叫一次 CLI，讀回它**解析成什麼**。那就是「這一族今天
#      最新的那個」，正是我們要的答案。
#
# **探測的成本是零個 token，不是「很少」。** claude 那側的 `system`/`init` 事件在
# 任何請求送出**之前**就印出來，裡面帶著解析後的完整 model id；讀到那一行就把行程
# 砍掉。codex 那側更乾脆：它先把表頭（含 `model:` 那一行）印出來、**再**去讀 stdin
# 的提示詞，所以只要一個字都不寫進 stdin，讀到表頭就砍，連請求都不會成形。
# 每天的代價因此是 5 次行程啟動（四個 claude 族 ＋ 一次 codex），各幾秒鐘。
#
# 失敗一律只記 stderr、留著昨天的目錄、明天再試——離線、額度用完、CLI 不在，
# 都不該比「今天沒更新」更嚴重。
DOROSSI_MODEL_PROBE_TIMEOUT_SEC = 90.0
# codex 表頭裡的模型那一行（`model: gpt-5.6-sol`）。
_DOROSSI_CODEX_MODEL_LINE_RE = re.compile(r"^\s*model:\s*(\S+)\s*$")


def _dorossi_model_probe_dir() -> Path:
    """探測用的空白工作目錄。

    刻意**不是** repo root 也不是工作階段的目錄：CLI 會讀 cwd 底下的指示檔，拿一個
    空目錄探測才快、也才不會把探測混進任何一段對話的紀錄裡。放在
    `DOROSSI_CC_WORKDIR` 底下，所以沿用既有的 gitignore 條目。
    """
    path = DOROSSI_CC_WORKDIR / ".model_probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dorossi_model_from_init_line(raw: bytes) -> str | None:
    """串流 JSON 的一行 → 那是不是 `system`/`init`，是的話解析後的完整 model id。

    抽成純函式是為了測得動：兩支探測的**整個判斷**就在這一行上，而起一個真的 CLI
    子行程來測它既慢又要網路。回 None 有兩種意思（不是 init／看不懂），呼叫端都是
    「繼續讀下一行」，所以不必分。永不 raise——餵進來的是別的行程印出來的位元組。
    """
    try:
        event = _json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # pylint: disable=broad-except
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") != "system" or event.get("subtype") != "init":
        return None
    model = event.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _dorossi_model_from_header_line(raw: bytes) -> str | None:
    """另一個 CLI 的表頭一行 → `model:` 印的是什麼（不是那一行就 None）。永不 raise。"""
    match = _DOROSSI_CODEX_MODEL_LINE_RE.match(raw.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def _dorossi_model_probe_argv(exe: str, family: str) -> list:
    """claude 探測用的 argv。純函式。

    **刻意不共用 `_dorossi_cc_argv`**，理由有兩條，都是安全性不是潔癖：那一支會在
    `dorossi_cc_tools == "full"` 時帶上 `--permission-mode bypassPermissions`，而一個
    只為了讀一行 init 就開全權限的子行程沒有任何理由存在；它也會附上整份系統提示，
    讓「讀第一行就砍掉」這件事變得不必要地重。這裡只要三件事：串流 JSON（才有 init
    事件）、裸別名（才看得到解析結果）、零工具零 MCP（才啟動得快）。
    """
    return [exe, "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", family,
            # 主機的全域 MCP 設定會讓 `claude -p` 在冷啟動健康檢查上卡好幾分鐘。
            "--strict-mcp-config",
            # 工具白名單，而且是空的（fail-closed）。探測不需要任何工具。
            "--tools", ""]


async def _dorossi_probe_claude_family(exe: str, family: str, cwd: str,
                                       timeout_sec: float) -> str | None:
    """叫一次 `claude -p --model <裸別名>`，讀回 init 事件裡解析後的完整 model id。

    讀到就立刻砍掉行程——init 在請求之前，所以這一趟是零 token。任何失敗回 None。
    """
    args = _dorossi_model_probe_argv(exe, family)
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd, env=_dorossi_cc_child_env(timeout_sec),
        limit=1024 * 1024)
    try:
        # `-p` 一定要有提示詞（空 stdin 會被當成用法錯誤），但我們在它被送出之前
        # 就收工了。
        try:
            proc.stdin.write(b"ping\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = await asyncio.wait_for(proc.stdout.readline(), remaining)
            if not raw:
                return None
            model = _dorossi_model_from_init_line(raw)
            if model:
                return model
    except (asyncio.TimeoutError, TimeoutError):
        print(f"[dorossi] model probe timed out for family {family!r}",
              file=sys.stderr)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model probe failed for family {family!r}: {exc!r}",
              file=sys.stderr)
        return None
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        await _dorossi_reap_proc(proc)


async def _dorossi_probe_codex_default(exe: str, cwd: str,
                                       timeout_sec: float) -> str | None:
    """叫一次 `codex exec`，讀回表頭那一行 `model:` 印的是什麼。

    這是 codex 這側唯一的發現管道——它的 `--json` 串流裡沒有任何地方講模型
    （2026-09-23 實測），而不認得的模型名不會退回預設，是伺服器 400。所以**刻意不加
    `--json`**：表頭只印在人讀的那個格式裡。

    ⚠️ 兩件事都是量出來才知道的，寫在這裡免得下一個人再踩一次：

      1. **提示詞一定要先寫進 stdin。** CLI 是先把 stdin 讀完才印表頭的，不寫就一路
         等到逾時（第一版刻意不寫，想連提示詞都不送，實測 90 秒全部用完）。
      2. **表頭印在 stderr，不是 stdout。** 在終端機上 `2>&1` 看不出差別，所以第一版
         盯著 stdout 讀，讀到的只有整輪跑完之後的那一行答案——等於每天白跑一整個回合。
         改讀 stderr 之後，`model:` 在請求送出之前就到手，砍掉行程即可，零 token。
    """
    args = [exe, "exec", "--skip-git-repo-check", "-C", str(cwd),
            "-c", 'sandbox_mode="read-only"', "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd), limit=1024 * 1024)
    try:
        try:
            proc.stdin.write(b"ping\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = await asyncio.wait_for(proc.stderr.readline(), remaining)
            if not raw:
                return None
            model = _dorossi_model_from_header_line(raw)
            if model:
                return model
    except (asyncio.TimeoutError, TimeoutError):
        print("[dorossi] model probe timed out for the other backend",
              file=sys.stderr)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model probe failed for the other backend: {exc!r}",
              file=sys.stderr)
        return None
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        await _dorossi_reap_proc(proc)


def _dorossi_api_credentials_present() -> bool:
    """主機上有沒有 SDK 用得到的憑證。**只看有沒有，永遠不印值。**"""
    return any(str(os.environ.get(name) or "").strip()
               for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))


async def _dorossi_probe_api_catalog(timeout_sec: float) -> dict:
    """有憑證時走 SDK 的模型清單：一次拿到整份目錄，比探測便宜也比探測完整。

    回傳 `{族名: 完整 id}`，只留內建表裡有的那幾族、每族取清單裡最新的一筆（SDK 的
    清單是新的在前）。沒有憑證／SDK 不在／任何失敗都回空字典，由呼叫端退回探測。
    """
    if not _dorossi_api_credentials_present():
        return {}
    cli = _get_dorossi_client()
    if cli is None:
        return {}
    try:
        page = await asyncio.wait_for(cli.models.list(limit=100), timeout_sec)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model list unavailable: {type(exc).__name__}",
              file=sys.stderr)
        return {}
    newest: dict = {}
    for item in (getattr(page, "data", None) or []):
        model_id = getattr(item, "id", None)
        family, _version = _dorossi_split_model_id(model_id)
        if family in DOROSSI_MODEL_PROBE_FAMILIES and family not in newest:
            newest[family] = str(model_id)
    return newest


async def dorossi_probe_model_catalog(
        timeout_sec: float | None = None) -> dict:
    """跑一次發現（不寫檔、不改表），回傳 `{命名空間: {族名: 完整 id}}`。

    抽出來是為了讓「發現」與「落地」分開測：這一支碰外部世界，下面那支只碰檔案與
    那兩張表。**會 raise**：個別探測讀不到東西時在裡面吞掉，但建探測目錄與起子行程
    （執行檔在 `which` 之後不見了、權限不足）的 `OSError` 會丟出來，由
    `dorossi_refresh_model_catalog` 接住。
    """
    if timeout_sec is None:
        timeout_sec = DOROSSI_MODEL_PROBE_TIMEOUT_SEC
    resolved: dict = {}
    claude_found = await _dorossi_probe_api_catalog(timeout_sec)
    exe = _shutil.which("claude")
    if not claude_found and exe:
        cwd = str(_dorossi_model_probe_dir())
        for family in DOROSSI_MODEL_PROBE_FAMILIES:
            model_id = await _dorossi_probe_claude_family(
                exe, family, cwd, timeout_sec)
            if model_id:
                claude_found[family] = model_id
    if claude_found:
        resolved["claude"] = claude_found
    codex_exe = find_codex_executable()
    if codex_exe:
        model_id = await _dorossi_probe_codex_default(
            codex_exe, str(_dorossi_model_probe_dir()), timeout_sec)
        family, _version = _dorossi_split_model_id(model_id)
        if family and model_id:
            resolved["codex"] = {family: str(model_id)}
    return resolved


def dorossi_model_catalog_due(interval_hours: float,
                              now: float | None = None) -> bool:
    """距離上次檢查夠久了嗎。上次的時刻存在目錄檔裡，所以**重啟不會重跑**。

    存放檔裡的時刻比現在還晚（時鐘被調過、檔案從別台機器複製過來）一律視為到期——
    否則一個未來的時刻會把這個檢查永久關掉，而且沒有任何症狀。`NaN` 同理：
    `json.loads` 收得下它，而它跟任何數字比都是 False，會讓下面每一道比較都落空。
    """
    now = time.time() if now is None else now
    last = _DOROSSI_MODEL_CATALOG.get("checked_at")
    if (not isinstance(last, (int, float)) or not math.isfinite(last)
            or last <= 0 or last > now):
        return True
    return (now - last) >= max(0.0, float(interval_hours)) * 3600.0


async def dorossi_refresh_model_catalog(
        timeout_sec: float | None = None) -> list:
    """發現 → 落地 → 併回表，回傳**這次新增的別名**（可能是空的）。

    回傳值是公告的素材，所以只會是別名，完整 id 一個字都不會被帶出這一層。
    探測全軍覆沒時**不動**已經存著的目錄（留著昨天的結果、明天再試）；部分成功就只
    更新成功的那幾族。永不 raise。

    探測丟例外也照「全軍覆沒」處理，**`checked_at` 照樣蓋上**。原本這條路直接回傳、
    不落地，而節流看的正是 `checked_at`，於是起不了子行程的那一天變成每分鐘重試一次
    ——每一次都印一行 stderr，而且都卡住健康迴圈直到失敗為止。
    """
    global _DOROSSI_MODEL_CATALOG  # pylint: disable=global-statement
    try:
        resolved = await dorossi_probe_model_catalog(timeout_sec)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[dorossi] model catalog probe failed: {exc!r}", file=sys.stderr)
        resolved = {}
    merged = {}
    previous = _DOROSSI_MODEL_CATALOG.get("resolved")
    if isinstance(previous, dict):
        for namespace, found in previous.items():
            if isinstance(found, dict):
                merged[namespace] = dict(found)
    for namespace, found in resolved.items():
        merged.setdefault(namespace, {}).update(found)
    catalog = {
        "schema": DOROSSI_MODEL_CATALOG_SCHEMA,
        "checked_at": time.time(),
        "resolved": merged,
    }
    dorossi_save_model_catalog(catalog)
    _DOROSSI_MODEL_CATALOG = catalog
    return dorossi_merge_model_catalog(catalog)


_DOROSSI_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# 後端產圖工具的輸出根目錄。**模組層常數是刻意的**：`_collect_codex_images` 產出的
# 路徑會被 bot 記進 `recent_image_msgs.json`，而 bot 重新載入那份檔案時必須判斷
# 「這個字串落在允許的根目錄底下嗎」——那道包含性檢查用的正是同一個根。兩邊各寫
# 一次字面值就是「同一條規則兩份實作」，其中一份遲早會漂掉，而漂掉的症狀是**靜默
# 的**：合法的對應在下次啟動時被默默丟掉，🗑️／⭐ 就此失效，沒有任何錯誤。
# 這裡刻意**不** `.resolve()`——兩邊的消費者各自 resolve，才能同時吸收 junction /
# symlink 與大小寫差異。
CODEX_IMAGE_ROOT = Path.home() / ".codex" / "generated_images"


def _collect_codex_images(thread_id: str | None, since_ns: int, *,
                          root: Path | None = None) -> list[str]:
    """Return images generated in this invocation for one Codex thread.

    Codex's image tool writes under ``~/.codex/generated_images/<thread-id>``.
    Restricting discovery to that exact directory and to new regular image
    files prevents arbitrary paths mentioned by model text from becoming
    Discord attachments.
    """
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", thread_id):
        return []
    # `root=` 是測試的注入點，保留；沒注入時走模組層的單一來源常數。
    base = (root or CODEX_IMAGE_ROOT).resolve()
    folder = (base / thread_id).resolve()
    if base not in folder.parents or not folder.is_dir():
        return []
    found: list[tuple[int, str]] = []
    try:
        for path in folder.iterdir():
            if (not path.is_file()
                    or path.suffix.lower() not in _DOROSSI_IMAGE_SUFFIXES):
                continue
            stat = path.stat()
            if stat.st_mtime_ns >= since_ns:
                found.append((stat.st_mtime_ns, str(path)))
    except OSError:
        return []
    found.sort()
    return [path for _mtime, path in found]


# --- 折疊事件時的兩個防禦性取值 -------------------------------------------
#
# 兩個 `feed` 的 docstring 都承諾「**永遠不 raise**」，而那個承諾是承重的：claude
# 那側的讀取迴圈**沒有** try/finally，例外會逸出整支函式 → 子行程沒人 kill、stderr
# 抽水任務變孤兒、那個 session 的鎖永久卡住，無人值守迴圈就地停住且沒有任何訊號。
#
# 2026-09-08 對兩個 feed 的每個事件骨架、每個巢狀位置逐一塞入各種 JSON 進得來的
# 非預期值（`None` / 數字 / 字串 / `[]` / `[1]` / `true` / `{}` / 小數）掃了一遍，
# 發現**三種**互相獨立的機制。一次修一個點正是這個 bug 類別會原地復發的原因，所以
# 這兩個 helper 是拿來一次擋掉整個類別的——新增欄位時請沿用，不要再寫 `X or {}`
# 或直接把外部值丟進 set。
#
#   (A) 對非 dict 呼叫 `.get()`。慣用法 `X or {}` 只擋得掉 **falsy**（`None`/`{}`），
#       擋不掉 **truthy 的非 dict**（`5`、`"字串"`、`[1]`）→ AttributeError。
#       → 一律走 `_event_dict()`。
#   (B) 把不可雜湊的值放進 set。JSON 的 array/object 會變成 `list`/`dict`，
#       `set.add()` **與 `set.discard()`** 都會丟 TypeError。
#       → 一律走 `_protocol_key()`。
#   (C) 輸入本身不是 str。`json.loads(None)` 丟的是 **TypeError**，不是 ValueError。
#       → 解析的 except 同時收 `(ValueError, TypeError)`（見兩個 feed）。


def _event_dict(obj: dict, key: str) -> dict:
    """`obj[key]`，**不是 dict 就回 `{}`**。取代 `obj.get(key) or {}`（機制 A）。"""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _protocol_key(value):
    """可以安全當成 set 元素的協定識別字，認不得就回 `None`（呼叫端丟掉）。

    只收 `str` 與 `int`——那正是協定本身用的型別（content block 的 `index` 是整數、
    `tool_use` 的 `id` 是字串）。**丟掉而不是轉字串**，兩個理由：

    * 轉換會**發明資料**。`str([1])` 產出的 `"[1]"` 是一個永遠不會被**格式正確**的
      後續事件配對到的幽靈鍵；`pending_tools` 留著配不掉的項目會一直抑制閒置監看
      （雖然硬上限仍會兜底），而丟掉的失敗方向是「監看可能早一點開火」——有界、而且
      會帶著診斷訊息大聲失敗。**安靜地抑制守門，比大聲地早一點開火糟得多。**
    * `add` 與 `discard` 用同一道過濾，所以兩邊一致：認不得的 id 不會進去，也就不需要
      被移除。

    **`bool` 被排除**（`isinstance(True, int)` 是 True，所以要明寫）：`index: true`
    會讓 `True` 進 `text_block_indices`，而 `1 in {True}` 成立——於是 index 為 1 的
    **工具**區塊的 delta 會被當成已知的文字區塊。那是進度預覽白名單的安全性質被
    別名繞過，不只是型別潔癖。同理，這道過濾也順手擋掉 `None`（欄位缺漏時
    `.get()` 的回傳值），否則「缺 index 的文字區塊」與「缺 index 的工具區塊」會互相
    別名。
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (str, int)) else None


class _CodexStreamState:
    """一次 `codex exec --json` 串流的累積狀態。分開的理由與 `_ClaudeStreamState`
    相同：讀取那一半綁死在 subprocess ＋ 看門狗上，折疊這一半是純資料轉換。"""

    def __init__(self, session_id: str | None = None) -> None:
        self.thread_id = session_id
        self.answer = ""
        self.usage: dict = {}
        # 失敗事件的文字，rc!=0 時用來分類（見 `_codex_stream_verdict`）。
        self.failure_texts: list = []

    def feed(self, raw_line: str, on_text=None) -> None:
        """把一行原始 stdout 折疊進狀態。**永遠不 raise**，也不做任何 I/O。

        與 Claude 那側同一個選擇：解不開的行**跳過、繼續讀**，不中止整輪。
        """
        try:
            event = _json.loads(raw_line)
        except (ValueError, TypeError):
            return  # 非 JSON 雜訊，或 `raw_line` 根本不是 str/bytes（機制 C）
        if not isinstance(event, dict):
            # 合法 JSON 但不是物件（`null` / 數字 / 陣列）：舊版會在 `event.get(...)`
            # 丟 AttributeError，讓呼叫端拿到一個非型別化的例外（abort／resume／
            # 用量上限全都分類不到）。跟雜訊同樣處理。
            return
        etype = event.get("type")
        if etype == "thread.started":
            self.thread_id = event.get("thread_id") or self.thread_id
        elif etype == "item.completed":
            item = _event_dict(event, "item")
            if item.get("type") == "agent_message" and item.get("text"):
                self.answer = str(item["text"]).strip()
                if on_text is not None:
                    try:
                        on_text(self.answer)
                    except Exception:  # pylint: disable=broad-except  # nosec B110
                        pass  # 進度更新失敗絕不影響主串流
        elif etype == "turn.completed":
            self.usage = _event_dict(event, "usage")
        else:
            # 失敗類事件的文字要留下來。codex 的用量上限／伺服器錯誤訊息不一定
            # 會出現在 stderr（走 JSON 事件時常常只在事件裡），而 rc!=0 的分類就靠
            # 這段文字——收不到就會退回舊行為：白白重開一個新工作階段，然後把整個
            # 無人值守任務判死。事件型別名稱在不同 codex 版本間會變（本機是
            # 0.145.0），所以這裡**不寫死型別**，只要事件裡帶了 error／message／
            # text 欄位就收，寧可多收也不要漏。
            for key in ("error", "message", "text", "reason"):
                val = event.get(key)
                if isinstance(val, dict):
                    val = val.get("message") or val.get("text")
                if isinstance(val, str) and val.strip():
                    self.failure_texts.append(val.strip()[:500])
                    break


def _codex_stream_verdict(state: "_CodexStreamState", rc: int, err_text: str,
                          session_id: str | None) -> str:
    """判定一次**已經結束**的 `codex exec` 串流：回傳 `"ok"` 或 raise。

    分類順序與 Claude 那一側完全一致，而且**順序就是規則本身**：
      用量上限 → 暫時性故障 → 連不上伺服器 → （最後才是）丟掉工作階段重開的
      resume 重試。
    resume 重試對「額度用完」與「伺服器過載」都毫無幫助，只是再燒一次呼叫、又把可以
    續接的對話丟掉；2026-09-05 之前 codex 這一側**只有**那條路。

    判定文字同時吃 stderr 與 JSON 失敗事件：codex 走事件輸出時，上限訊息常常只出現
    在事件裡，stderr 是空的。
    """
    if rc == 0:
        return "ok"
    print(f"[dorossi] codex exited rc={rc}: {err_text[:1000]}", file=sys.stderr)
    blob = "\n".join([err_text] + list(state.failure_texts))
    usage_exc = _dorossi_codex_usage_limit(blob, state.thread_id)
    if usage_exc is not None:
        raise usage_exc
    transient_exc = _dorossi_codex_transient(blob, state.thread_id)
    if transient_exc is not None:
        raise transient_exc
    offline_exc = _dorossi_codex_offline(blob, session_id)
    if offline_exc is not None:
        raise offline_exc
    if session_id:
        raise _DorossiResumeError("codex resume failed")
    raise RuntimeError("codex invocation failed")


def _dorossi_codex_argv(exe: str, *, session_id: str | None = None,
                        workdir: str | None = None,
                        extra_dir: str | None = None,
                        model: str | None = None,
                        tools_mode: str | None = None) -> list:
    """`codex exec` 的 argv（含 `exe`）。純函式：不碰檔案、不起行程、不讀環境。

    從 `_dorossi_via_codex` 抽出來（2026-09-23，行為不變——除了新增的 `-m`），理由
    與 `_dorossi_cc_argv` 同一條：argv 是這條路上唯一「使用者輸入有機會變成 CLI 參數」
    的地方，抽成純函式才測得動每一種組合。

    `model` 是 `dorossi_resolve_model` 查表命中的**完整 model id**；None ＝不帶
    `-m`、用 CLI 自己的預設。**使用者輸入永不原樣走到這裡**——這個參數的每一個可能
    值都來自 allowlist 查表的結果。

    `tools_mode` 省略時在**呼叫當下**讀模組全域 `DOROSSI_CC_TOOLS`（同
    `_dorossi_cc_argv`）。判準仍是「剛好等於 `full` 才解鎖」，其餘一律重申唯讀沙箱。
    """
    if tools_mode is None:
        tools_mode = DOROSSI_CC_TOOLS
    args = [exe, "exec"]
    if session_id:
        args += ["resume", "--json"]
    else:
        args += ["--json", "--skip-git-repo-check", "-C", str(workdir or "")]
        if extra_dir:
            args += ["--add-dir", extra_dir]
    if model:
        # 每輪 `/model` 覆蓋。`exec` 與 `exec resume` 都收 `-m/--model`（2026-09-23
        # 在 CLI 0.145.0 實測兩個子指令的 help 都列著它）。不帶就是 CLI 預設。
        args += ["-m", model]
    if tools_mode == "full":
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        # `exec resume` does not expose `--sandbox`, but it does accept config
        # overrides. Reassert read-only on every invocation rather than relying
        # on whatever the host's user config happens to contain.
        args += ["-c", 'sandbox_mode="read-only"']
    args += ([session_id, "-"] if session_id else ["-"])
    return args


async def _dorossi_via_codex(
        prompt: str, session_id: str | None, on_text=None,
        extra_dir: str | None = None, workdir: str | None = None,
        on_proc=None, abort_check=None, silence_limit: float | None = None,
        loop_system_guidance: str | None = None,
        model: str | None = None) -> tuple:
    """Run one non-interactive Codex turn; return answer, thread id and usage.

    `model` ＝這一輪 `/model` 解析出來的完整 model id（None ＝不帶旗標）。
    2026-09-23 之前這條路**完全不帶 `-m`**，所以 `/model` 對 codex 是安靜失效的。"""
    exe = find_codex_executable()
    if exe is None:
        raise FileNotFoundError("codex CLI not found on PATH")
    effective_cwd = workdir or str(DOROSSI_CC_WORKDIR)
    try:
        cwd_path = Path(effective_cwd)
        # 只有落在我們自己管的子樹裡才自動建目錄；判準是單一決策點，不要在
        # 這裡自己再寫一次 `.parents` 比對。
        if _dorossi_cwd_is_managed(cwd_path):
            cwd_path.mkdir(parents=True, exist_ok=True)
    except Exception:  # pylint: disable=broad-except
        pass
    # 存著的目錄可能早就不在了：在 spawn 之前講清楚，而不是讓子行程丟一個會被判成
    # 「CLI 無法使用」的 NotADirectoryError。見 `_dorossi_require_workdir`。
    _dorossi_require_workdir(effective_cwd)

    # argv 由 `_dorossi_codex_argv` 組（純函式），**不要在這裡再寫一份**。
    args = _dorossi_codex_argv(
        exe, session_id=session_id, workdir=effective_cwd,
        extra_dir=extra_dir, model=model)
    turn_prompt = prompt
    if loop_system_guidance:
        turn_prompt += "\n\n" + loop_system_guidance
    wire_prompt = turn_prompt if session_id else (
        DOROSSI_SYSTEM_PROMPT + "\n\n使用者問題：\n" + turn_prompt)

    invocation_started_ns = time.time_ns()
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=effective_cwd,
        limit=16 * 1024 * 1024)
    # 回呼／stdin 一律包起來（與 _dorossi_via_claude_code 對齊）：這些都是「輔助動作」，
    # 失敗絕不可把整輪打掉。尤其 abort_check 命中時會先 kill 行程，緊接著的 stdin 寫入
    # 必然 BrokenPipe——若不吞掉，例外會在下面 try/finally「之外」逸出，stderr_task 變成
    # 沒人 await 的孤兒任務，且呼叫端拿到的是非型別化的例外（abort 路徑該走的是 kill →
    # EOF → rc!=0）。
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    if abort_check is not None:
        try:
            if abort_check():
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    stderr_task = asyncio.create_task(_read_stream_all(proc.stderr))
    try:
        proc.stdin.write(wire_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:  # pylint: disable=broad-except
        pass
    state = _CodexStreamState(session_id)
    clock = _DorossiWatchClock()        # 扣掉主機睡著的時間（見 `_dorossi_readline_watched`）
    deadline = clock.now() + _dorossi_cc_hard_limit_sec()
    try:
        while True:
            timeout = (silence_limit if silence_limit is not None
                       else deadline - clock.now())
            if timeout <= 0:
                raise TimeoutError("codex hard limit exceeded")
            raw = await _dorossi_readline_watched(proc.stdout, timeout, clock)
            if not raw:
                break
            state.feed(raw.decode("utf-8", errors="replace"), on_text)
        wait_timeout = (silence_limit if silence_limit is not None
                        else max(1.0, deadline - clock.now()))
        rc = await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # 有上限地收（不要裸的 `await proc.wait()`）：kill 之後管線若仍被孫行程握著，
        # `wait()` 要等 EOF 才會回來，也就是永遠不會——見 `_dorossi_reap_proc`。
        await _dorossi_reap_proc(proc)
        if silence_limit is not None:
            raise _DorossiLoopSilence("codex loop output silence")
        raise TimeoutError("codex invocation timed out")
    finally:
        # 先確保子行程真的結束，才能 await stderr_task。`_read_stream_all` 讀到 EOF 才
        # 回來，而 EOF 只在子行程結束（管線關閉）後才發生——所以只要有任何「非預期」
        # 離開路徑（回呼丟例外、任務被取消、readline 超出緩衝上限）讓行程還活著就
        # 直接 await，這裡會**永遠卡住**，而且是在 wait_for 之外、沒有任何看門狗守著，
        # 等於握著該 session 的鎖永久掛死。正常路徑（rc 已取得或上面已 kill 過）
        # returncode 已設定，這段是 no-op。
        #
        # **2026-09-09 更正：光是「先 kill 再 await」還不夠。** 實測（CPython 3.14 /
        # Windows）`await proc.wait()` 自己就是無限的——它要等**所有管線 EOF**，而
        # `proc.kill()` 是 `TerminateProcess`、帶不走繼承了管線的孫行程。所以 kill
        # 之後的等待與 stderr 的抽水**兩個都要有上限**，逾時之後把任務收乾淨。
        # 同步的 kill 保留在這裡（不要挪進 `_dorossi_reap_proc` 的「先等再殺」順序）：
        # 非預期離開路徑上我們不保證還有機會跑完任何 await。
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
        await _dorossi_reap_proc(proc)
        err_text = await _dorossi_drain_stderr(stderr_task)
    _codex_stream_verdict(state, rc, err_text, session_id)
    return state.answer, state.thread_id, {
        "usage": state.usage,
        "images": _collect_codex_images(state.thread_id, invocation_started_ns),
    }


class _ClaudeStreamState:
    """一次 `claude -p --output-format stream-json` 串流的累積狀態。

    **為什麼要跟叫用分開**：讀取那一半綁死在 subprocess ＋ 兩段式看門狗 ＋
    `proc.kill()` 上，要測就得真的起一個後端；折疊這一半純粹是「事件 → 狀態」的
    資料轉換，抽出來就能用假串流餵。錯誤分類（用量上限／暫時性過載／輸出靜默）
    要用的資料全部由這裡累積，而那三條路正是 2026-09-05 事故的現場——判定本身在
    `_claude_stream_verdict`。

    `kill_reason` 由**讀取端**設定（看門狗砍掉行程時），不是折疊端設的：折疊端看
    不到時間，也不該看得到。None 代表「串流自己走到 EOF」——注意那**不等於**成功，
    後端被砍掉、管線斷掉同樣是 EOF，區分它們的是 rc（見 `_claude_stream_verdict`）。
    """

    _STDOUT_TAIL_LINES = 25

    def __init__(self, session_id: str | None = None) -> None:
        self.sid = session_id
        self.answer = ""
        # 串流進度（單則訊息 live 預覽）狀態：累積「答案文字」的 text_delta。
        # 只累積 type=="text" 的 content block；tool_use 的 input_json_delta 不算，
        # 也不會被送到 Discord——進度只反映已淨化的答案文字。
        self.stream_text = ""
        self.text_block_indices: set = set()   # 已知為 text 型別的 content block index
        self.pending_tools: set = set()        # 已發出但尚未收到 result 的 tool_use id
        # CLI 目前回報的背景工作（`system`/`background_tasks_changed` 的 task_id）。
        # 與 `pending_tools` 一樣是閒置看門狗的抑制條件：模型可以在回合結尾留下一個
        # 還沒觸發的監看工作，CLI 先送出 `result`、然後**完全靜默**地等它觸發，再重新
        # 叫模型、送第二則 `result`。背景工作不是 pending 的 tool_use，所以只看
        # `pending_tools` 會把這段等待當成閒置砍掉（2026-09-19 事故）。
        self.background_tasks: set = set()
        # 在 `--output-format stream-json` 模式下，`claude -p` 失敗時會把錯誤寫進
        # STDOUT 的 `result` 事件（而非 stderr）。保留最後一個 result 事件物件，
        # 以及一段有界的 stdout 尾巴，供 rc != 0 時診斷真正原因。
        self.last_result_ev: dict = {}
        self.stdout_tail: list = []
        # 本次呼叫最後一則 `rate_limit_event` 給的重設時刻（epoch 秒）。撞到用量上限時
        # 用它決定要睡多久——這是唯一結構化、來自伺服器配額標頭的來源。
        self.rate_reset: float | None = None
        # 同一則事件的 status（allowed / allowed_warning / rejected）。用途只有一個：
        # 否決成功回合裡的文字判定（見 `_dorossi_cc_limit_text_counts`）。
        self.rate_status: str | None = None
        # init 事件的 `claude_code_version`。唯一用途：判斷這次 `--resume` 回報的
        # `total_cost_usd`／`modelUsage` 是每次叫用還是工作階段累計（2.1.277 起是後者，
        # 見 `_dorossi_cc_account_round`）。bot 每輪重新起 CLI，CLI 又會自動更新，所以
        # 版本要**每一次叫用**從串流裡讀，不能在 bot 啟動時問一次就算數。
        self.cli_version: str | None = None
        # init 事件有沒有 `memory_paths` 這個鍵（None ＝還沒看到 init）。實測（2.1.276）
        # 一般模式兩種工具模式都有、`--bare`／`CLAUDE_CODE_SIMPLE=1` 整個鍵不存在——
        # 那是「CLI 沒讀登入、沒讀指示檔」唯一看得到的結構化訊號（`apiKeySource` 在
        # bare 與一般模式都是 "none"，分不出來）。用在啟動警告與未登入那一行診斷。
        self.init_memory_paths: bool | None = None
        # init 事件的 `apiKeySource`（CLI 的來源**標籤**，例如 "none"／"ANTHROPIC_API_KEY"，
        # 不是金鑰本身）。"none" ＝走登入；其他值 ＝改用 API key 計費。只給啟動警告用，
        # 印之前還要過 `_DOROSSI_API_KEY_SOURCE_LABEL_RE`。
        self.api_key_source: str | None = None
        # 最後一則 `system`/`api_retry` 的錯誤類別與狀態碼（CLI 自己分好的類，詞彙見
        # `_DOROSSI_AUTH_RETRY_ERRORS` 上方）。**每一則都覆寫**：後來一次非驗證類的重試
        # 要能蓋掉前面那次驗證類的，否則早先一次暫時的 401 會讓整輪被判成未登入。
        self.retry_error: str | None = None
        self.retry_status: int | None = None
        self.kill_reason: str | None = None    # None / "idle" / "hard" / "silence"

    @staticmethod
    def _content_blocks(ev: dict) -> list:
        """`ev["message"]["content"]`，任何不是 list 的形狀一律回 `[]`（機制 A/B）。

        `ev.get("message", {})` 的預設值**只在鍵不存在時生效**——鍵在而值是 `null`
        時不會用到它，於是 `.get("content")` 丟 AttributeError；`content` 是數字時
        `for blk in 5` 丟 TypeError。兩者都違反 `feed` 的「永遠不 raise」承諾。
        """
        blocks = _event_dict(ev, "message").get("content")
        return blocks if isinstance(blocks, list) else []

    @staticmethod
    def _background_task_ids(tasks) -> set:
        """`background_tasks_changed` 的 `tasks` 快照 → task_id 集合（機制 A/B）。

        那個事件帶的是**完整快照**，不是增量，所以呼叫端整個取代舊集合。形狀不對
        （`tasks` 不是 list、項目不是 dict、`task_id` 不可雜湊或缺漏）一律丟掉，
        **不是保留舊值**：理由同 `_protocol_key`——認不得的快照若讓舊集合留著，會
        安靜地一直抑制閒置看門狗；丟掉的失敗方向是「看門狗可能早一點開火」，有界而且
        大聲（`_claude_stream_verdict` 在已經拿到成功答案時還會把答案留下）。
        """
        if not isinstance(tasks, list):
            return set()
        ids = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = _protocol_key(task.get("task_id"))
            if task_id:
                ids.add(task_id)
        return ids

    def feed(self, raw_line: str, on_text=None) -> None:
        """把一行原始 stdout 折疊進狀態。**永遠不 raise**，也不做任何 I/O。

        壞行的處置是**跳過、繼續讀**，不是中止整輪：串流裡混進非 JSON 雜訊
        （CLI 的警告、被截斷的半行）是常態，而為了一行雜訊丟掉整輪已經跑完的工作
        代價高得多。真正的失敗訊號是 rc 與 `result` 事件，不是某一行解不開。
        """
        if raw_line:
            self.stdout_tail.append(raw_line)
            if len(self.stdout_tail) > self._STDOUT_TAIL_LINES:
                del self.stdout_tail[0]
        try:
            ev = _json.loads(raw_line)
        except (ValueError, TypeError):
            # ValueError = 非 JSON 雜訊（含空行、被截斷的半行）。
            # TypeError  = `raw_line` 根本不是 str/bytes（機制 C）：`json.loads(None)`
            #              丟的是 TypeError，只收 ValueError 會讓它逸出。
            return
        if not isinstance(ev, dict):
            # 合法 JSON 但不是物件（`null` / 數字 / 陣列）。舊版直接 `ev.get(...)`，
            # 那會丟 AttributeError，而 claude 這一側的讀取迴圈**沒有** try/finally
            # ——例外會逸出整支函式，子行程與 stderr 抽水任務都沒人收，等於漏掉一個
            # 行程還握著那個 session 的鎖。跟雜訊同樣處理。
            return
        etype = ev.get("type")
        if etype == "stream_event":
            # 來自 --include-partial-messages 的逐 token 串流。只取「答案文字」的
            # text_delta（type=="text" 的 content block）累積給 on_text；
            # tool_use 的 input_json_delta 不取，確保進度不外洩工具呼叫內容。
            sev = _event_dict(ev, "event")
            stype = sev.get("type")
            # 兩個分支都以這個索引當鍵。`None` 代表「認不得的索引」——**兩邊都丟掉**，
            # 所以登記與比對永遠對稱（見 `_protocol_key` 的別名說明）。
            index = _protocol_key(sev.get("index"))
            if stype == "content_block_start":
                blk = _event_dict(sev, "content_block")
                if blk.get("type") == "text" and index is not None:
                    self.text_block_indices.add(index)
            elif stype == "content_block_delta":
                delta = _event_dict(sev, "delta")
                if delta.get("type") == "text_delta" \
                        and index is not None \
                        and index in self.text_block_indices:
                    piece = delta.get("text") or ""
                    if piece:
                        self.stream_text += piece
                        if on_text is not None:
                            try:
                                on_text(self.stream_text)
                            except Exception:  # pylint: disable=broad-except  # nosec B110
                                pass  # 進度更新失敗絕不影響主串流
            return
        if etype == "rate_limit_event":
            # 每一次呼叫都會來一則，帶著這個配額視窗真正的重設時刻。留最後一則：
            # 撞上限時就能**睡到那一刻**，而不是走「15 分鐘起跳、每次加倍」的猜。
            # 見 `_dorossi_rate_limit_reset`。
            got = _dorossi_rate_limit_reset(ev)
            if got is not None:
                self.rate_reset = got
            # status **每一則都覆寫**（讀不到就變 None），不像上面那樣保留上一則的值：
            # 前一則說 allowed、這一則是讀不懂的拒絕時，留著舊的 allowed 會否決掉一則
            # 真的通知。None 的失敗方向是「不否決」，退回文字判定。
            self.rate_status = _dorossi_rate_limit_status(ev)
            return
        if etype == "system":
            if ev.get("session_id"):
                self.sid = ev["session_id"]
            # 真實串流的 system 事件都帶 session_id，所以這一條**不能**寫成上面那個
            # 分支的 elif，否則背景工作快照永遠讀不到。
            if ev.get("subtype") == "init":
                version = ev.get("claude_code_version")
                if isinstance(version, str) and version.strip():
                    self.cli_version = version.strip()[:64]
                # 看的是「鍵在不在」，不是值：bare 模式整個鍵不存在（見 __init__）。
                self.init_memory_paths = "memory_paths" in ev
                source = ev.get("apiKeySource")
                self.api_key_source = source[:64] if isinstance(source, str) else None
            if ev.get("subtype") == "api_retry":
                self.retry_error, self.retry_status = _dorossi_api_retry_fields(ev)
            if ev.get("subtype") == "background_tasks_changed":
                self.background_tasks = self._background_task_ids(ev.get("tasks"))
        elif etype == "assistant":
            for blk in self._content_blocks(ev):
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                tool_id = _protocol_key(blk.get("id"))
                if tool_id:
                    self.pending_tools.add(tool_id)
        elif etype == "user":
            for blk in self._content_blocks(ev):
                if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                    continue
                # `discard` 跟 `add` 一樣會對不可雜湊的值丟 TypeError，所以移除這一側
                # 也要過同一道濾網——而且**必須是同一道**，否則登記得進去、卻移除不掉。
                tool_id = _protocol_key(blk.get("tool_use_id"))
                if tool_id:
                    self.pending_tools.discard(tool_id)
        elif etype == "result":
            self.last_result_ev = ev  # 保留整個事件（含 subtype/is_error/...）供診斷
            # 只有字串才算答案。非字串（`null`／數字／物件）代表這個事件沒有可用的
            # 答案文字，而不是「答案是它的 repr」——原始事件整份留在 `last_result_ev`
            # 裡，分類（用量上限／預算閘）照樣看得到它。
            raw_answer = ev.get("result")
            self.answer = raw_answer.strip() if isinstance(raw_answer, str) else ""
            if ev.get("session_id"):
                self.sid = ev["session_id"]

    def failure_reason(self, stderr_tail: str = "") -> str:
        """rc != 0 時組出的診斷字串（只進 stderr，不進對話平台）。

        在 stream-json 模式下 `claude -p` 把錯誤寫到 STDOUT 的 result 事件而非
        stderr，所以來源依序是：result 事件 → stdout 尾巴 → stderr。
        """
        if self.last_result_ev:
            parts = []
            for key in ("subtype", "is_error", "api_error_status", "result"):
                val = self.last_result_ev.get(key)
                if val not in (None, ""):
                    parts.append(f"{key}={val}")
            reason = "; ".join(parts) if parts else ""
        else:
            reason = ""
        if not reason:
            # `str(x)`：機制 C 修好之後非 str 的行也進得了 `stdout_tail`，
            # 而 `join` 對非 str 元素會丟 TypeError——診斷路徑同樣不該炸。
            reason = "\n".join(str(x) for x in self.stdout_tail)[-4000:]
        if not reason:
            reason = stderr_tail[-400:]
        if not reason:
            reason = "(no output)"
        return reason


# CLI 指令列剖析器拒絕不認得的選項時印的那一句（2026-09-19 用 2.1.276 餵一個不存在的
# 選項實測：`error: unknown option '--tools-bogus-xyz'`、rc=1、stdout 全空）。捕捉組刻意
# 收斂成「看起來像一個旗標」的形狀，所以印進 log 的只會是旗標名，不會是任意文字。
_CLI_UNKNOWN_OPTION_RE = re.compile(r"unknown option '(--?[A-Za-z0-9][A-Za-z0-9_-]{0,63})'")


def _dorossi_cc_rejected_option(state: "_ClaudeStreamState",
                                stderr_tail: str) -> str | None:
    """rc != 0 時：CLI 是不是**在開始這一輪之前**就拒絕了我們傳的某個選項？是就回那個
    選項名，否則 None。純函式、永不 raise。

    兩個條件都要成立：串流裡**沒有**任何 `result` 事件（有的話回合已經開始了，失敗是
    別的原因——那些照舊走後面的分類），而且 stderr 有剖析器那一句。只看 stderr 不夠：
    一個正常開始的回合，它的 stderr 裡剛好出現那串字，不該被說成「CLI 太舊」。"""
    try:
        # 「沒有 result」在這個狀態物件上是**空 dict**，不是 None（`__init__` 設的就是
        # `{}`，`failure_reason` 用的也是真值判斷）——寫成 `is not None` 會讓這支永遠回 None。
        if state.last_result_ev:
            return None
        match = _CLI_UNKNOWN_OPTION_RE.search((stderr_tail or "")[-4000:])
        return match.group(1) if match else None
    except Exception:  # pylint: disable=broad-except
        return None


# ---- 後端 CLI 沒有可用的登入（2026-09-19 實測後補） -------------------------
#
# 實測（CLI 2.1.276）三種形狀，前兩種是這一段要認的，第三種是 resume 重試**真正**該管的：
#
#   | 情境 | rc | 串流 | result |
#   |---|---|---|---|
#   | `--bare`（或 `CLAUDE_CODE_SIMPLE=1`），環境沒有 key | 1（1.1 秒）| init（**沒有** `memory_paths`）、assistant、result | `subtype` 仍是 "success"、`is_error` 真、`api_error_status` null、`terminal_reason` "api_error"、文字「Not logged in · Please run /login」 |
#   | `--bare` ＋ 無效的 `ANTHROPIC_API_KEY` | 1（**190 秒**）| 十則 `system`/`api_retry`（`error` "authentication_failed"、`error_status` 401），再 assistant、result | `api_error_status` **401**、文字「Failed to authenticate. API Error: 401 API key is invalid.」 |
#   | `--resume <不存在的 id>` | 1 | result | `subtype` "error_during_execution"、`result` null、`errors` ["No conversation found …"] |
#
# 判準依證據強度排：**結構化欄位優先，文字最後**，文字還要過兩道（與
# `_dorossi_cc_limit_text_counts` 同一個教訓——比對得很寬的字樣只能用在錯誤訊息上，
# 不能用在答案上）。
_DOROSSI_AUTH_STATUSES = frozenset({401})
# `api_retry` 的 `error` 是 CLI 自己分好的類（2.1.276 執行檔內的 SDK schema：
# authentication_failed、oauth_org_not_allowed、account_on_hold、verification_required、
# billing_error、rate_limit、overloaded、invalid_request、model_not_found、server_error、
# unknown、max_output_tokens、cloud_credential_error）。CLI 自己把其中五類標成「卡住、要
# 人處理」；這裡只收**憑證**那三類——另外兩類（帳號凍結、需要驗證）也不會自己好，但
# 「在主機上登入」是錯的處方，那兩類照舊走有上限的重試。billing_error 屬用量／付費，
# 由用量上限那條（402）管。
_DOROSSI_AUTH_RETRY_ERRORS = frozenset({
    "authentication_failed", "oauth_org_not_allowed", "cloud_credential_error"})
# 只有文字、沒有結構化欄位時的退路（bare 模式「Not logged in」就是這樣：狀態碼 null、
# 沒有 api_retry）。小寫比對。
_DOROSSI_AUTH_TEXT_MARKERS = ("not logged in", "please run /login", "failed to authenticate")
# 文字退路的長度上限。CLI 的通知是一行模板（實測兩句 33 與 58 字）；會談到 /login 的
# 答案是散文。上限與用量上限那條同值、理由同。
_DOROSSI_AUTH_NOTICE_MAX_CHARS = 300


def _dorossi_api_retry_fields(ev) -> tuple:
    """`system`/`api_retry` 事件 → (錯誤類別, 狀態碼)。認不得的一律 None。永不 raise。

    類別只收字串並截短（只拿來比對成員、印的是比對後的固定詞彙）；狀態碼只收真的整數
    （`True` 是 int 的子類，要排除）。"""
    try:
        error = ev.get("error")
        status = ev.get("error_status")
    except Exception:  # pylint: disable=broad-except
        return None, None
    error = error[:64] if isinstance(error, str) else None
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    return error, status


def _dorossi_cc_auth_failure(result_ev, *, retry_error: str | None = None,
                             retry_status: int | None = None) -> str | None:
    """rc != 0 的這一輪，是不是「CLI 沒有可用的登入」？是就回證據標籤，否則 None。
    純函式、永不 raise。

    依序：

    1. **成功完成的 result 一律不是**（`_claude_result_succeeded`）。早先一次 api_retry
       說 401、後來重試成功、答案也出來了，而行程因為別的原因非零離開——那不是未登入。
    2. result 的 `api_error_status` 是 401 → 是。**403 不算**：未實測，而且它是
       permission_error，可能只是這個方案用不到某個模型（`/model` 換一個就好）或中間有
       代理擋掉，「在主機上登入」會是錯的處方；判準的保守方向是「可重試」（見
       `dorossi_error_is_fatal`）。
    3. 最後一則 api_retry 的類別是憑證類 → 是，但 result 的狀態碼必須是空的或 403：
       result 是**最後**發生的事，它說了別的狀態碼（400、404…）就以它為準，早先的重試
       事件不能蓋過它。403 在這裡放行，是因為這時 CLI 自己的分類（讀的是錯誤本文，不只
       是狀態碼）已經說它是憑證問題。
    4. 前面都沒有結構化證據（狀態碼空的）時才看文字：`is_error` 為真、文字不超過
       `_DOROSSI_AUTH_NOTICE_MAX_CHARS`、含 `_DOROSSI_AUTH_TEXT_MARKERS` 其中之一。

    回傳的標籤只由固定詞彙組成（狀態碼數字、白名單裡的類別名、"result text"），不含
    CLI 的原始文字——它會進 stderr，而 stderr 有 `/log tail` 這個出口。
    """
    try:
        if _claude_result_succeeded(result_ev):
            return None
        ev = result_ev if isinstance(result_ev, dict) else {}
        raw_status = ev.get("api_error_status")
        try:
            status = (int(raw_status) if raw_status not in (None, "")
                      and not isinstance(raw_status, bool) else None)
        except (TypeError, ValueError):
            status = None
        if status in _DOROSSI_AUTH_STATUSES:
            return f"api_error_status={status}"
        if retry_error in _DOROSSI_AUTH_RETRY_ERRORS and status in (None, 403):
            suffix = f"/{retry_status}" if isinstance(retry_status, int) else ""
            return f"api_retry={retry_error}{suffix}"
        if status is not None:
            return None
        text = ev.get("result")
        if ev.get("is_error") and isinstance(text, str):
            clean = text.strip()
            if (clean and len(clean) <= _DOROSSI_AUTH_NOTICE_MAX_CHARS
                    and any(m in clean.lower() for m in _DOROSSI_AUTH_TEXT_MARKERS)):
                return "result text"
    except Exception:  # pylint: disable=broad-except
        return None
    return None


# `apiKeySource` 標籤印出來之前要長得像一個標籤（實測／文件裡的值："none"、
# "ANTHROPIC_API_KEY"、"apiKeyHelper"、"/login managed key"）。形狀不對就整個不印——
# 那個欄位哪天改成帶值，也漏不出來。與 `verify_dorossi_cli` 共用這一支。
_DOROSSI_API_KEY_SOURCE_LABEL_RE = re.compile(r"[A-Za-z0-9_./ -]{1,40}")
_DOROSSI_BARE_MODE_WARNING = (
    "[dorossi] claude -p started without instruction-file discovery (its init event has "
    "no memory_paths). That looks like bare mode - CLAUDE_CODE_SIMPLE set somewhere, or a "
    "CLI whose -p now defaults to --bare: the subscription login and the instruction "
    "files are skipped, so calls either fail as not logged in or are billed to an API key.")


def _dorossi_cc_startup_warnings(state: "_ClaudeStreamState") -> list:
    """這一次叫用的 init 事件透露出「CLI 沒照這個後端的前提啟動」的話，回要印的句子。
    純函式（印由呼叫端交給 `_warn_once`，所以每個行程每句只出現一次）。

    兩件事，都只在**看到了 init** 時才判（沒有 init ＝判斷不出來，不是警報）：

    * init 沒有 `memory_paths` → 像 bare 模式。實測一般模式在純聊天與 full 兩種工具模式
      下都有這個鍵（2026-09-19，本機 2.1.276，用 bot 自己的 argv），所以正常運作時不會
      亂叫。
    * `apiKeySource` 不是 "none" → CLI 改用 API key 計費。本行程已經不把 API key 變數交給
      子行程（`_DOROSSI_CC_DROPPED_ENV`），所以還出現就代表來源在 CLI 自己的設定裡（設定
      檔的 env 區塊、apiKeyHelper）。實測以訂閱憑證變數 `CLAUDE_CODE_OAUTH_TOKEN` 登入時
      仍是 "none"，不會亂叫。
    """
    messages = []
    if getattr(state, "init_memory_paths", None) is False:
        messages.append(_DOROSSI_BARE_MODE_WARNING)
    source = getattr(state, "api_key_source", None)
    if isinstance(source, str) and source != "none":
        shown = (source if _DOROSSI_API_KEY_SOURCE_LABEL_RE.fullmatch(source)
                 else "(an unrecognised source label)")
        messages.append(
            f"[dorossi] claude -p is authenticating with {shown} instead of the "
            "subscription login (apiKeySource in its init event): billed per token, with "
            "no plan usage limit. This process does not pass API-key variables to it, so "
            "check the CLI's own settings (an env block or apiKeyHelper).")
    return messages


def _claude_result_succeeded(result_ev) -> bool:
    """這則 `result` 事件是不是一個**成功完成**的回合（`subtype == "success"` 且
    `is_error` 不為真）。形狀不對一律當成沒有成功——失敗方向是回到舊行為（丟例外）。"""
    return (isinstance(result_ev, dict)
            and result_ev.get("subtype") == "success"
            and not result_ev.get("is_error"))


def _claude_stream_verdict(state: "_ClaudeStreamState", rc: int, err: str,
                           session_id: str | None, *,
                           silence_limit: float | None = None,
                           idle_limit: float = 0.0,
                           hard_limit: float = 0.0) -> str:
    """判定一次**已經結束**的 `claude -p` 串流是什麼結果：回傳字串或 raise。

    回傳 `"ok"`（照常回答）或 `"budget"`（每次叫用的預算閘被觸發，graceful 收尾、
    不重試、不拋例外）——兩者呼叫端走同一條回傳路徑。其餘一律 raise 型別化例外。

    **判定順序就是規則本身，不要重排**（2026-09-05 事故：三個具名分支被排到泛用處
    理後面，整段變死碼而沒有任何訊號）：

      輸出靜默 → 硬性上限 → 閒置 → 預算閘 → 用量上限 → 暫時性故障
      → 沒有可用的登入 → CLI 拒絕旗標 → 連不上伺服器 → resume 重試。

    最後那條（丟掉工作階段重開）之所以排最後：它對「額度用完」「伺服器過載」「沒登入」
    與「CLI 不認得我們傳的旗標」都毫無幫助，只是再燒一次呼叫、又把可以續接的對話丟掉。
    「沒有可用的登入」排在用量上限與暫時性故障之後，讓 429／402／5xx 照舊走各自的等待
    （兩邊都成立的輸入由測試釘住）。「CLI 拒絕旗標」只在串流裡連一則 `result` 都沒有時
    成立，所以它與預算閘、用量上限、暫時性故障湊不出同時成立的輸入；與「沒有可用的
    登入」只在 api_retry 那條證據上湊得出來，而串流裡有 api_retry 就代表 CLI 已經開始
    打後端、旗標顯然收下了，所以登入排在它前面。

    **三種看門狗砍法共用一個出口**：被砍掉、但串流裡**已經**收到成功的 `result` 時，
    那一輪其實已經答完了，只是 CLI 之後還掛著（例如等一個背景工作觸發）。這時不丟
    例外，直接走 rc==0 那條收尾（含用量上限攔截），**不**
    落到後面的 rc != 0 分類——被砍掉的行程 rc 一定非零，落下去會被誤判成失敗或觸發
    resume 重試。2026-09-19 事故：答案早在 300 秒前就出來了，使用者看到的卻是「暫時
    無法回應」。
    這個出口起初**只給閒置**；同一天擁有者交辦「任務一直被殺掉」，而上限拉長之後
    （full 硬上限 3 小時、背景工作會把閒置與自走沉默一路壓到硬上限），一個不觸發的
    背景工作可以把一輪撐滿 3 小時再**丟掉早就出來的答案**——所以硬上限與自走沉默也
    收下答案。邊界照舊：**砍的時刻完全不變**（出口只改「砍完之後怎麼判」），沒有成功
    的 result 時三種照舊丟例外。自走沉默收下答案之後迴圈照常把它當完成的一輪，而不是
    沉默重試——重試會把已經做完的那一輪整個重跑一次。

    **EOF 不等於成功。** 後端被砍、管線斷掉、abort 落地都是 EOF；分辨它們的是 rc。
    rc != 0 而串流連一則 `result` 事件都沒有時，`failure_reason` 會退到 stdout 尾巴
    ／stderr，診斷不會變成空字串。

    `session_id` 是**這次叫用傳進去的**那一個（不是 `state.sid`）：resume 重試該不該
    觸發，取決於我們是不是在 resume，而不是後端最後回報了哪個 id。
    """
    # 看門狗出口（見 docstring）：三種砍法共用。答案已經在手上、只是 CLI 答完之後還掛著
    # 時，**不**丟例外，也**跳過**下面的 rc != 0 分類（被砍掉的行程 rc 一定非零，落下去
    # 會被當成失敗或觸發 resume 重試），直接走 rc==0 那條收尾——用量上限攔截照樣會跑。
    answered = _claude_result_succeeded(state.last_result_ev)
    if state.kill_reason == "silence":
        limit = silence_limit or 0.0
        if not answered:
            # 背景工作數要印出來：非零代表這一輪的沉默原本是被背景工作壓住的，砍掉它的
            # 是那段壓制的牆鐘上限（`hard_limit`），不是一般的沉默——兩者要分得出來。
            print(f"[dorossi] claude -p loop-silence-killed: no output for "
                  f"{limit:.0f}s (pending tools={len(state.pending_tools)}, background "
                  f"tasks={len(state.background_tasks)}, round ceiling while "
                  f"background tasks run={hard_limit:.0f}s); "
                  f"stderr tail: {err[-300:]!r}", file=sys.stderr)
            raise _DorossiLoopSilence(
                f"claude -p produced no output for {limit:.0f}s")
        print(f"[dorossi] claude -p lingered after its final result; loop-silence-"
              f"killed after {limit:.0f}s (background tasks="
              f"{len(state.background_tasks)}), answer kept", file=sys.stderr)
    elif state.kill_reason == "hard":
        if not answered:
            # Report the SAME limit used for this run's deadline (mode-aware,
            # config-driven), not a hardcoded number.
            print(f"[dorossi] claude -p hard-killed: exceeded {hard_limit:.0f}s "
                  f"wall-clock ceiling (pending tools={len(state.pending_tools)}, "
                  f"background tasks={len(state.background_tasks)}); stderr tail: "
                  f"{err[-300:]!r}", file=sys.stderr)
            raise TimeoutError(
                f"claude -p exceeded {hard_limit:.0f}s hard wall-clock limit")
        print(f"[dorossi] claude -p lingered after its final result; hard-killed "
              f"at the {hard_limit:.0f}s wall-clock ceiling (pending tools="
              f"{len(state.pending_tools)}, background tasks="
              f"{len(state.background_tasks)}), answer kept", file=sys.stderr)
    elif state.kill_reason == "idle":
        if not answered:
            print(f"[dorossi] claude -p idle-killed: no output for {idle_limit:.0f}s "
                  f"and no tool running; stderr tail: {err[-300:]!r}", file=sys.stderr)
            raise TimeoutError(
                f"claude -p idle for {idle_limit:.0f}s (no output, no shell running)")
        print(f"[dorossi] claude -p lingered after its final result; idle-killed "
              f"after {idle_limit:.0f}s, answer kept", file=sys.stderr)
    elif rc != 0:
        reason = state.failure_reason(err)
        print(f"[dorossi] claude -p exited {rc}: {reason}", file=sys.stderr)
        # 每輪預算閘（--max-budget-usd）被觸發：這不是工作階段過舊、也不是方案用量
        # 上限，而是我們自己設的「單次 invocation 花費上限」。**必須在 resume 重試／
        # 用量上限判定之前**先攔截：一律 graceful 收尾——不重試（會再燒一次預算）、
        # 不拋例外，讓呼叫端回傳目前的（多半為空的）答案＋session id，自走迴圈把這
        # 一輪當「無進展(idle)」由 consecutive_idle 吸收、下一輪 resume 續跑。預算
        # 金額／旗標一律不進 Discord，只記 stderr。
        if _dorossi_cc_budget_exceeded(state.last_result_ev):
            print("[dorossi] claude -p hit per-invocation budget cap "
                  "(--max-budget-usd); treating round as idle, no retry.",
                  file=sys.stderr)
            return "budget"
        # 方案／配額用量上限不是「工作階段過舊」的問題，所以必須在觸發 resume
        # 重試（會再燒掉一次呼叫）之前先攔截。用量上限要回報專屬例外。
        usage = _dorossi_cc_usage_limit(
            state.last_result_ev, state.answer, state.sid,
            stream_reset=state.rate_reset, stream_status=state.rate_status)
        if usage is not None:
            raise usage
        # 伺服器側的暫時性故障（529 Overloaded、5xx）。**必須排在 resume 重試之
        # 前**：那個重試是把工作階段丟掉重開一個，對「伺服器過載」毫無幫助，只是
        # 再燒一次呼叫、又把可以續接的對話丟掉。這裡改成往上拋專屬例外，讓自走
        # 迴圈退避等待後**用同一個工作階段**重跑。
        transient = _dorossi_cc_transient_error(
            state.last_result_ev, state.answer, state.sid)
        if transient is not None:
            raise transient
        # CLI 沒有可用的登入（沒登入、憑證失效、被迫進 bare 模式）。**必須排在 resume 重試
        # 之前**：重開一個工作階段會以一模一樣的方式失敗，而且把可以續接的對話丟掉；也要排
        # 在用量上限與暫時性故障**之後**，讓 429／402／5xx 照舊走它們自己的等待路徑。
        # 對外照舊泛用（`_dorossi_error_hint` 的致命分支）；stderr 這一行要把處方講對。
        auth = _dorossi_cc_auth_failure(
            state.last_result_ev, retry_error=state.retry_error,
            retry_status=state.retry_status)
        if auth is not None:
            bare = state.init_memory_paths is False
            hint = (" The CLI also started without instruction-file discovery (no "
                    "memory_paths in its init event): this looks like bare mode - "
                    "CLAUDE_CODE_SIMPLE set somewhere, or a CLI whose -p now defaults to "
                    "--bare - in which the subscription login is never read." if bare else "")
            print(f"[dorossi] claude -p has no usable sign-in ({auth}). Retrying cannot "
                  f"help: sign in on the host (run the CLI interactively and use /login) "
                  f"and make sure no API-key variable or setting overrides that login. Not "
                  f"retrying with a fresh session (it would fail the same way).{hint}",
                  file=sys.stderr)
            raise _DorossiAuthError(auth, bare_suspect=bare)
        # CLI 在開始之前就拒絕了我們傳的旗標（多半是 CLI 比這份程式碼舊）。**必須排在
        # resume 重試之前**：重開一個工作階段會以一模一樣的方式失敗。stderr 這一行要把
        # 原因講對——上面那行 `exited 1: error: unknown option …` 讀起來像一個普通的後端
        # 錯誤，看不出該做的事是更新 CLI。對外照舊泛用（由 bot 的 `_dorossi_error_hint` 組）。
        rejected = _dorossi_cc_rejected_option(state, err)
        if rejected is not None:
            print(f"[dorossi] claude -p refused to start: the installed CLI does not "
                  f"know the option {rejected!r} that this code passes. It is probably "
                  f"older than this code expects - update it. Not retrying with a fresh "
                  f"session (it would fail the same way).", file=sys.stderr)
            raise _DorossiCliOptionError(rejected)
        # 連不上伺服器（DNS、連線被拒／重設）。**必須排在 resume 重試之前**：丟掉工作
        # 階段重開一個對斷網毫無幫助（新的那次一樣連不上），只會把可以續接的對話丟掉
        # ——2026-09-22 的斷網就是這樣讓六個自走任務全部丟了脈絡再停掉。排在其他具名
        # 分支之後：用量上限／暫時性故障／未登入都有自己更準的處理，它們成立時不該被
        # 讀成斷網。呼叫端會等網路回來、用**同一個**工作階段重跑。
        offline = _dorossi_cc_offline(state.last_result_ev, err, session_id)
        if offline is not None:
            print("[dorossi] claude -p cannot reach its server (network); the caller "
                  "will wait for connectivity and resume the same session",
                  file=sys.stderr)
            raise offline
        # `--resume` 的 run（session_id 有值）失敗時一律觸發一次性的新工作階段
        # 重試——不再依賴 stderr 是否含 'resume'/'session' 關鍵字，因為真正的
        # 失敗訊號出現在 stdout 而非 stderr（過大／過舊的工作階段無法 resume）。
        if session_id:
            raise _DorossiResumeError(reason)
        raise RuntimeError(f"claude -p exited {rc}: {reason}")
    # rc == 0（或上面的看門狗出口）：少數情況下用量上限會以 rc==0 回來，且 result 文字
    # 本身就是上限通知而非真正的回答——這裡也要攔截，否則會把上限通知當成正常答覆送出。
    # **但成功的答案本身不是通知**：一篇剛好討論到 rate limit 的長答案曾在這裡被判成
    # 用量上限、整份丟掉（2026-09-17、09-19）。文字證據要過
    # `_dorossi_cc_limit_text_counts`（串流說放行 → 否決；否則要短得像一則通知）。
    usage = _dorossi_cc_usage_limit(
        state.last_result_ev, state.answer, state.sid,
        stream_reset=state.rate_reset, stream_status=state.rate_status)
    if usage is not None:
        raise usage
    return "ok"


def _dorossi_cc_argv(exe: str, *, session_id: str | None = None,
                     model: str | None = None, effort: str | None = None,
                     max_budget_usd: float | None = None,
                     loop_system_guidance: str | None = None,
                     extra_dir: str | None = None,
                     tools_mode: str | None = None) -> list:
    """`claude -p` 的 argv（`exe` 之後全部）。純函式：不碰檔案、不起行程、不讀環境。

    從 `_dorossi_via_claude_code` 抽出來（2026-09-19，行為不變——抽之前與抽之後對 480 種
    參數組合組出的 argv 逐位元組相同），理由只有一個：手動驗證入口
    `verify_dorossi_cli.py` 要拿**同一份** argv 去打真的 CLI。它自己抄一份的話，驗到的是
    那份副本，而這裡哪天多一個旗標它不會知道。

    `tools_mode` 省略（None）時在**呼叫當下**讀模組全域 `DOROSSI_CC_TOOLS`，與抽出來之前
    一樣（測試靠換掉那個全域切模式）。判準仍是「剛好等於 `"full"` 才解鎖」，其餘一律走
    純聊天的兩層封鎖——fail-closed 的方向不因為多了這個參數而改變。其他參數的語意見
    `_dorossi_via_claude_code` 的 docstring。
    """
    if tools_mode is None:
        tools_mode = DOROSSI_CC_TOOLS
    args = [
        exe, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        # 每輪 `/model` 覆蓋（已由解析端對照成後端別名並驗證）；未指定維持預設。
        "--model", (model or DOROSSI_CC_MODEL),
        # Load ZERO MCP servers (none passed via --mcp-config). The host's
        # global MCP config can cold-start health-checks that hang `claude -p`
        # for minutes inside the bot.
        "--strict-mcp-config",
        # 把系統提示裡「每機動態區段」（cwd／env／git status）移到第一則 user 訊息，
        # 讓系統提示前綴在跨行程 resume 時保持穩定 → 提示快取前綴更常命中（這些動態
        # 區段每次叫用可能變動而讓快取前綴失配）。實測與下方 --append-system-prompt
        # 併用 CLI 接受、exit 0（help 雖寫「with --system-prompt」，但本碼用的是
        # --append-system-prompt，預設系統提示仍在用，故此旗標適用）。無條件帶上。
        "--exclude-dynamic-system-prompt-sections",
    ]
    if effort:
        # 每輪 `/effort` 覆蓋思考力度；未指定就完全不帶旗標（維持 CLI 預設）。
        args += ["--effort", effort]
    if max_budget_usd is not None and max_budget_usd > 0:
        # 每一次 invocation 的美元花費上限（只在 --print 路徑有效，本來就是）。
        # 超出時 `claude -p` 以非零 exit ＋ result 事件 subtype=="error_max_budget_usd"
        # 回報，由 `_claude_stream_verdict` 的 rc!=0 區塊 graceful 收尾（不重試、不拋例外）。
        args += ["--max-budget-usd", str(max_budget_usd)]
    if tools_mode == "full":
        # 完整 agent：開啟所有工具並移除核准關卡（擁有者授權）。
        args += ["--permission-mode", "bypassPermissions"]
    else:
        # 純聊天（預設）：Dorossi 只能對話，無法執行 shell／讀寫檔案。**兩層，順序不重要、
        # 缺一層就不是這裡描述的東西：**
        #   1. `--tools ""`——工具**白名單**，而且是空的。這一層是 fail-closed：CLI 之後
        #      新增的工具不會自己出現在純聊天裡。2026-09-19 在 CLI 2.1.276 實測：init 事件
        #      的工具列表是 0 個，聊天照常回答；一個 full 模式建立、歷史裡有 tool_use 的
        #      工作階段用它 resume 也照常回答（模式互切不必重設工作階段）。
        #   2. `--disallowedTools …`——舊的**列舉黑名單**，留著當第二層。它單獨存在時是
        #      fail-open 的：同一天實測只帶它時 init 仍列出 18～22 個工具（隨 cwd 而異），
        #      其中 ListAgents（列出本機其他互動式工作階段）與 SendMessage 不經核准就執行
        #      得到。`--restricted` 也不夠（仍留 14 個）。
        # 空字串必須是 argv 裡**一個真的空元素**：這裡走 exec、不經 shell，Windows 上
        # `list2cmdline` 把它寫成 `""`，CLI 讀回來就是空的工具清單（同日以正式程式碼實跑
        # 一次確認，見 `test_dorossi_stream` 那段；`verify_dorossi_cli.py` 的第 1 項檢查
        # 每次都再量一次 init 的工具列表）。舊版 CLI 若不認得 `--tools`，會在開始前以
        # 「unknown option」拒絕——`_dorossi_cc_rejected_option` 把它講清楚。
        args += [
            "--tools", "",
            "--disallowedTools",
            "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
        ]
    # 系統提示通道（穩定、可快取、不累積進對話歷史）。實測 --append-system-prompt 與
    # --resume 併用「該輪生效、且不會被 baked 進 session」，所以：
    #   * 新工作階段（無 session_id）：補上基底系統提示（含保密守則）——append（非取代），
    #     保留 Claude Code 的 agent／工具 scaffolding。resume 會保留原本的，不再重補。
    #   * loop_system_guidance（自走耐久守則，僅自走迴圈傳入）：每一輪（含 resume／壓縮輪）
    #     都附上，確保守則每輪都實際到達後端、卻不像舊版那樣累積進 user-prompt 歷史。
    # 兩者併成單一 --append-system-prompt 一次帶上。
    append_parts: list[str] = []
    if not session_id:
        append_parts.append(DOROSSI_SYSTEM_PROMPT)
    if loop_system_guidance:
        append_parts.append(loop_system_guidance)
    if session_id:
        args += ["--resume", session_id]  # resume keeps the original system prompt
    if append_parts:
        args += ["--append-system-prompt", "\n\n".join(append_parts)]
    if extra_dir:
        # 本次對話的額外可存取目錄（開新對話時指定，往後每一輪都要重新帶上）。
        # 屬於 per-invocation 旗標，不會被 --resume 記住。
        args += ["--add-dir", extra_dir]
    return args


async def _dorossi_via_claude_code(
        prompt: str, session_id: str | None,
        on_text=None, extra_dir: str | None = None,
        workdir: str | None = None, silence_limit: float | None = None,
        on_proc=None, abort_check=None,
        max_budget_usd: float | None = None,
        loop_system_guidance: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        usage_baseline: dict | None = None) -> tuple:
    """Answer via one-shot headless Claude Code (`claude -p`), so usage rides
    the host login's plan. Continuity uses Claude Code's own session: pass the
    stored `session_id` to `--resume`; the returned id is stored for next time.
    Prompt on STDIN (no shell / no argv flag-parsing of user text), neutral
    temp cwd, MCP isolated.

    Tool exposure is config-gated by DOROSSI_CC_TOOLS (bot_config.json →
    dorossi_cc_tools): "off" (default) is pure chat — an EMPTY tool allowlist
    (`--tools ""`, fail-closed) plus the enumerated `--disallowedTools` denylist
    as a second layer, no bypassPermissions, so Dorossi cannot run shell or
    touch the host filesystem; "full" is the full agent — all tools enabled and
    the approval gate removed (--permission-mode bypassPermissions), owner-
    authorized. A session created in one mode resumes fine under the other.

    Streams NDJSON events (`--output-format stream-json`) and runs a two-tier
    watchdog: (1) IDLE — interrupt if there is no output for
    DOROSSI_CC_IDLE_LIMIT_SEC AND no tool/shell is currently executing, so a
    long-but-progressing answer (or a tool that keeps emitting output) keeps
    running; (2) HARD — always kill after the mode-aware, config-driven hard
    wall-clock ceiling (_dorossi_cc_hard_limit_sec(): off ~900s / full ~10800s
    by default, overridable in bot_config.json) regardless of pending tools.
    The idle limit is `dorossi_cc_idle_limit_sec` (default 600s); pending tools
    AND CLI-reported background tasks both hold it off. The hard
    ceiling is mandatory: in "full" mode (`--permission-mode bypassPermissions`)
    a tool can block forever, which would suppress the idle tier indefinitely
    and hang the handler with no reply; it stays in force in pure-chat mode too.
    Both kills raise TimeoutError so the caller replies generically — UNLESS a
    successful `result` was already received (the CLI answered, then lingered),
    in which case the answer is kept (see `_claude_stream_verdict`; the same
    exit applies to the loop-mode silence kill).

    `on_text`, if given, is a sync callback invoked with the accumulated ANSWER
    text (the concatenation of streamed `text_delta` deltas of the assistant's
    text content blocks) as it grows — used to drive a throttled single-message
    live preview. It carries ONLY the answer text (same sanitized content as the
    final reply), never tool_use input, tool results, or raw stdout. The
    authoritative answer is still the `result` event's `result` field.

    `extra_dir`, if given, is appended as `--add-dir <extra_dir>` to grant the
    backend an additional accessible directory for THIS invocation. It is a
    per-invocation flag — it is NOT baked into the resumed session, so the
    caller must re-pass it on every turn for the whole conversation. In off
    (pure-chat) mode it is inert (tools are disallowed); no special-casing.

    `workdir`, if given, becomes the subprocess `cwd` for THIS invocation,
    replacing the default DOROSSI_CC_WORKDIR — the backend really executes there
    (loading that directory's own config). Like `extra_dir` it is a
    per-invocation choice (the resumed session does NOT remember it; in fact
    Claude Code keys its session store by a cwd-hash, so the caller MUST pass the
    SAME workdir on every turn of a conversation or `--resume` would look in a
    different store and fail). It is validated as an existing directory when it
    is WRITTEN, but read back verbatim, so it may have vanished since:
    `_dorossi_require_workdir` re-checks it right before the spawn (after the
    managed-subtree mkdir) and raises `_DorossiWorkdirError`, instead of letting
    the child die with a `NotADirectoryError` that is misreported as "CLI
    unavailable". Auto-mkdir applies ONLY inside the managed DOROSSI_CC_WORKDIR
    subtree; a user-supplied workdir is never created, and is passed UNCHANGED.

    `silence_limit`, if given, switches the watchdog to AUTONOMOUS-LOOP mode: the
    normal two-tier watchdog (idle + mode-aware hard wall-clock ceiling) is
    REPLACED by a single output-silence backstop — if there is no new stdout
    line for `silence_limit` seconds the process is killed REGARDLESS of pending
    tools (the deliberate difference from the idle tier, so a hung tool can't
    suppress the watchdog forever) and `_DorossiLoopSilence` is raised. There is
    no round cap and no wall-clock ceiling on a round that keeps producing
    output (the loop driver bounds total runtime via the per-round silence kill
    + `@bot abort`); `silence_limit` is still finite and clamped ≥ 60s at the
    config layer so this protection can never be disabled.
    ONE exception (2026-09-19): while the CLI reports background tasks
    (`state.background_tasks` — a background subagent / shell / monitor the
    turn is waiting on), a silence timeout does NOT kill — but only until the
    round is `_dorossi_cc_hard_limit_sec()` old; the first silence timeout after
    that kills as before. The readline wait stays `silence_limit` throughout,
    so a kill can only ever land at or after the moment the old code would have
    killed (strictly more lenient, never earlier). When `silence_limit` is None
    (the default, normal path) the existing two-tier watchdog is unchanged.

    `on_proc`, if given, is a sync callback invoked once with the live subprocess
    right after it is spawned — the loop driver uses it to record the handle so
    `@bot abort` can kill the in-flight round.

    `abort_check`, if given, is a sync predicate checked once right after spawn:
    if it already returns True (an abort landed in the brief window between the
    caller's last flag check and this spawn) the process is killed immediately so
    no full unattended round runs after the user asked to stop.

    `max_budget_usd`, if given and > 0, is passed as `--max-budget-usd` — a
    per-INVOCATION (this one `claude -p` call) dollar cap, NOT a round/turn cap,
    so it is compatible with the autonomous loop's no-round-cap ruling. When the
    cap is hit the CLI exits non-zero with a `result` event whose
    subtype == "error_max_budget_usd" (no `result` answer); this function
    handles that GRACEFULLY — it does NOT raise and does NOT trigger the stale-
    session resume retry (which would re-spend the budget). It returns the
    (empty) answer + session id so the caller treats the round as idle and the
    next round resumes the same session. Budget amounts never reach Discord.

    `model` / `effort`, if given, override this invocation's backend model /
    reasoning effort (this function trusts them: `effort` is a validated level
    word, `model` is an allowlist-validated value from DOROSSI_MODEL_CHOICES
    resolved by `_dorossi_session_tuning` — raw user input never reaches this
    argument). At the CLI level both are per-INVOCATION
    flags (NOT baked into `--resume` — like `extra_dir`/`workdir` the caller
    must re-pass them on every call of the conversation); the SESSION-level
    persistence lives caller-side in the session store (`tune_effort`/
    `tune_model` on the slot, re-read into every snapshot), per the owner
    ruling that `/effort`・`/model` apply to the whole session until changed.
    None keeps the defaults (DOROSSI_CC_MODEL; no effort flag at all).

    `loop_system_guidance`, if given (autonomous loop only), is appended to the
    system prompt on EVERY call — fresh AND resume. Verified: --append-system-
    prompt applies on a --resume turn and is NOT baked into the stored session,
    so re-passing it each round keeps the durable loop rules (verify/tooling
    guidance) reaching the backend every round WITHOUT them accumulating into the
    growing user-prompt transcript (the O(N²) drain). Single-turn calls leave it
    None. Because a fresh loop round bakes (system+guidance) at creation and
    resume rounds re-append the guidance, a loop-created session's resume rounds
    carry the guidance twice in the system prompt — harmless (cached, never
    accumulated) and the deliberate cost of guaranteeing every round (incl. a
    session first created as a single-turn chat) actually receives the rules.

    `usage_baseline`, if given, is the slot's stored `cc_usage_mark` — the raw
    totals the CLI reported at the end of the previous call on the resumed
    session. CLI 2.1.277+ reports `--resume` totals cumulatively, so this is what
    turns them back into per-call numbers (`_dorossi_cc_account_round`). Ignored
    on a fresh session (no `session_id`).

    Returns (answer, session_id, info) where `info` is the per-INVOCATION
    round-info dict (`cost_usd` / `in` / `cr` / `cc` / `out`, plus `ctx` = the
    last API call's context size when readable, `acct` = how the numbers were
    derived, and `usage_mark` = the raw totals the caller must persist into the
    SAME slot as the next call's `usage_baseline`) so the autonomous loop can
    accumulate per-round spend for budget-awareness / the periodic-compaction
    trigger. The info numbers are diagnostics only — never sent to Discord."""
    exe = _shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("claude CLI not found on PATH")
    # argv 由 `_dorossi_cc_argv` 組（純函式），**不要在這裡再寫一份**：手動驗證入口
    # `verify_dorossi_cli.py` 用同一支組 argv 去打真的 CLI，這裡一分岔，它驗的就是一份
    # 副本（`test_verify_dorossi_cli` 釘住這一行）。
    args = _dorossi_cc_argv(
        exe, session_id=session_id, model=model, effort=effort,
        max_budget_usd=max_budget_usd, loop_system_guidance=loop_system_guidance,
        extra_dir=extra_dir)
    # 本次後端的工作目錄：使用者指定就用它（寫入時驗過存在，spawn 前再確認一次），
    # 否則用預設 workspace。使用者目錄不對它硬 mkdir。
    if workdir:
        effective_cwd = workdir
    else:
        effective_cwd = str(DOROSSI_CC_WORKDIR)
    # Create the workspace subtree on demand. mkdir ANY cwd inside our managed
    # DOROSSI_CC_WORKDIR (the shared default OR a per-session isolated subdir
    # passed as `workdir`), but NEVER a user-supplied external `/new <path>` dir —
    # that must already exist (re-checked just below) and must not be auto-made.
    try:
        cwd_path = Path(effective_cwd)
        # 只有落在我們自己管的子樹裡才自動建目錄；判準是單一決策點，不要在
        # 這裡自己再寫一次 `.parents` 比對。
        if _dorossi_cwd_is_managed(cwd_path):
            cwd_path.mkdir(parents=True, exist_ok=True)
    except Exception:  # pylint: disable=broad-except
        pass
    # 寫入時驗過不代表現在還在（見 `_dorossi_require_workdir`）；必須排在 mkdir 後面。
    _dorossi_require_workdir(effective_cwd)
    # 硬性牆鐘上限：mode-aware（off 較緊 / full 放大）＋ 可由 bot_config.json 覆寫。
    # 用函式解析目前模式對應的值，不要引用寫死的數字。**在 spawn 之前取一次**：同一個
    # 值既是下面看門狗的 deadline，也交給 CLI 當它自己的背景工作等待上限（見
    # `_dorossi_cc_child_env`），兩者必須是同一個數。loop_mode 下它只用來界定「背景
    # 工作壓住沉默看門狗」能壓多久，有輸出的回合照舊沒有牆鐘上限。
    hard_limit = _dorossi_cc_hard_limit_sec()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=effective_cwd,
        env=_dorossi_cc_child_env(hard_limit),
        limit=16 * 1024 * 1024,  # one `result` NDJSON line can be large
    )
    # 讓呼叫端記錄這個子行程（自走模式用來讓 `@bot abort` 即時 kill 當前回合）。
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    # 若在「呼叫端最後一次檢查旗標」與「這裡 spawn」之間 abort 才落地，立刻自我終止，
    # 不要讓一輪無人值守的工作在使用者已要求停止後還整輪跑完（下面 readline 會 EOF）。
    if abort_check is not None:
        try:
            if abort_check():
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    # Send the prompt, then close stdin so the CLI starts answering.
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:  # pylint: disable=broad-except
        pass
    # Drain stderr concurrently so a full stderr pipe can't deadlock the child.
    # **只能由下面的 `finally` 透過 `_dorossi_drain_stderr` 收**——它等的是 EOF，
    # 而 EOF 不在我們手上（見 `_dorossi_reap_proc` 的說明）。
    err_task = asyncio.ensure_future(_read_stream_all(proc.stderr))

    # 自走模式（silence_limit 有值）：停用一般兩段式看門狗，改用單一「輸出沉默」
    # backstop。否則維持原本的 idle ＋ 硬性牆鐘上限。
    loop_mode = silence_limit is not None
    idle = DOROSSI_CC_IDLE_LIMIT_SEC
    # `hard_limit` 在 spawn 之前就取好了（同一個值也交給了 CLI，見上方）。看門狗的時鐘
    # 扣掉主機睡著的時間（`_DorossiWatchClock`，見 `_dorossi_readline_watched`）。
    clock = _DorossiWatchClock()
    deadline = clock.now() + hard_limit  # 硬性牆鐘上限（不含休眠）
    state = _ClaudeStreamState(session_id)
    try:
        while True:
            if loop_mode:
                # 自走模式：每次 readline 最多等 silence_limit；沒有硬牆鐘上限、不設回合上限。
                wait = silence_limit
            else:
                # Bound每次 readline 的等待時間，使其不超過離硬上限剩餘的秒數。
                # 這樣即使 `if state.pending_tools: continue` 也只能 loop 到硬上限為止。
                remaining = deadline - clock.now()
                if remaining <= 0:
                    state.kill_reason = "hard"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                wait = min(idle, remaining)
            try:
                line = await _dorossi_readline_watched(proc.stdout, wait, clock)
            except asyncio.TimeoutError:
                if loop_mode:
                    # 背景工作還在（背景 subagent／shell／監看工作）：這段沉默是在等它，
                    # 不砍——但只撐到這一輪的牆鐘上限。**等待長度不改**（仍是
                    # silence_limit），所以砍的時刻只可能等於或晚於舊行為，不會提早；
                    # 改成 min(silence, 剩餘) 會在上限前夕把一個剛沉默不久的回合提早砍掉。
                    # 前景工具（pending_tools）刻意**不**算：卡死的前景工具正是這層
                    # backstop 要擋的東西。
                    if state.background_tasks and clock.now() < deadline:
                        continue
                    # 自走模式 backstop：在 silence_limit 內完全沒有新輸出 → 一律終止，
                    # 即使仍有工具在執行（與一般 idle tier 的關鍵差異：卡死的工具不會
                    # 永遠壓住看門狗）。視為卡住，由呼叫端的沉默重試接手。
                    state.kill_reason = "silence"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                # 這一段沒有輸出。先檢查是否已碰到硬上限——若是，不論有沒有 pending
                # 的工具都一律終止行程。
                if clock.now() >= deadline:
                    state.kill_reason = "hard"
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    break
                # 還沒到硬上限：若仍有工具/shell 在執行就繼續等，只有「真正閒置」
                # （無輸出且無工具執行）才會被閒置監看砍掉。CLI 回報的背景工作也算
                # （回合結尾留下的監看工作會讓 CLI 靜默等它觸發，2026-09-19 事故）；
                # 兩者都只能把等待撐到上面的硬上限為止。
                if state.pending_tools or state.background_tasks:
                    continue
                state.kill_reason = "idle"
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                break
            if not line:
                break  # EOF — 行程自己結束了。**注意這不等於成功**：被砍掉／管線斷掉
                # 也是 EOF，分辨它們的是下面的 rc（見 `_claude_stream_verdict`）。
            # 折疊（純資料轉換）搬進 `_ClaudeStreamState.feed`：讀取這一半綁死在
            # subprocess ＋ 看門狗上、測不動；折疊那一半可以用假串流餵。
            state.feed(line.decode("utf-8", "replace").strip(), on_text)
    except BaseException:  # pylint: disable=broad-except
        # 非預期離開（`readline` 丟的 `ValueError`／`ConnectionResetError`、外面打進來的
        # `CancelledError`）：**同步**先把行程砍掉，
        # 不要等下面 `_dorossi_reap_proc` 那個有上限的 wait。理由是取消路徑上我們不保證
        # 還有機會跑完任何 await——`finally` 裡的等待是盡力而為，`proc.kill()` 不是
        # coroutine，一定跑得完。正常路徑（`break`）不經過這裡，所以行程仍然有機會
        # 自己好好離開，不會被提早砍。
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        raise
    finally:
        # **每一條離開路徑都要走這裡**，不只是正常結束那一條。實際走得到而迴圈自己
        # 不處理的有兩類：(1) `readline()` 丟出非逾時的例外——單行 NDJSON 超過 16MB
        # 緩衝上限時 `readuntil` 的 `LimitOverrunError` 會被 `readline` 轉成
        # `ValueError`，管線斷掉則是 transport 設進 reader 的 `ConnectionResetError`；
        # 迴圈的 `except asyncio.TimeoutError` 兩個都接不到。(2) abort／關機從外面打
        # 進來的 `CancelledError`（`BaseException`，任何 `except Exception` 都接不到）。
        # （`on_text` 回呼丟例外**不在**這個名單裡：`_ClaudeStreamState.feed` 自己把它
        # 吞掉了，那是它「永遠不 raise」承諾的一部分，`test_dorossi_stream.py` 有釘。）
        # 舊版這裡沒有
        # try/finally，那三條路都會留下一個還在跑的後端行程（`full` 模式下它握著
        # 主機的 shell）＋一個沒人收的 stderr 抽水任務，而呼叫端此時正握著這個
        # session 的鎖——後續每一輪都只能排隊等一個永遠不會結束的回合。
        #
        # 兩個等待都有上限，理由寫在 `_dorossi_reap_proc`：`await proc.wait()`
        # 與 `await err_task` **各自**都是無限的，先前只有兩段式看門狗守著迴圈裡面，
        # 迴圈外面這兩行完全沒人守。
        rc = await _dorossi_reap_proc(proc)
        err = await _dorossi_drain_stderr(err_task)
    if rc is None:
        # 連 rc 都問不出來（kill 過了行程還在）。這是主機層的異常狀態，交給既有的
        # 「非零離開」分類去處理——診斷會退到 result 事件／stdout 尾巴。
        rc = _DOROSSI_UNREAPED_RC
        print("[dorossi] claude -p could not be reaped; "
              f"treating as rc={rc}", file=sys.stderr)
    # 啟動形狀的警報（像 bare 模式、改用 API key 計費）：排在判定**之前**，因為判定可能
    # raise——而這兩件事最常出現的時候正是那一輪失敗的時候。每句每個行程只印一次。
    for warning in _dorossi_cc_startup_warnings(state):
        _warn_once(warning)
    # 判定（純函式：raise 或回傳）與帳本寫入（副作用）分開。兩種「正常收尾」
    # ——"ok" 與預算閘 graceful 的 "budget"——走同一條回傳路徑；預算那條之所以
    # 不能落到下面的 rc==0 用量檢查，是因為 verdict 已經在它自己那一步 return 了。
    _claude_stream_verdict(state, rc, err, session_id,
                           silence_limit=silence_limit,
                           idle_limit=idle, hard_limit=hard_limit)
    return state.answer, state.sid, _dorossi_round_info_and_record(
        state.last_result_ev, stderr_tail=err,
        cli_command=_dorossi_cli_command_of(prompt),
        resumed_id=session_id, sid=state.sid, cli_version=state.cli_version,
        baseline=usage_baseline)
