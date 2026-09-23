"""這個行程在服務**哪一個平台**，以及它自己的狀態、鎖與記錄檔放在哪裡。

一個平台一個行程。啟動器對每個開著的平台各起一個受監督的子行程，彼此**沒有任何
共用的可變狀態**——所以一個平台崩潰、被重啟或被關掉，都不會碰到其他平台。

## 三個決定

1. **「我是哪個平台」只在這裡判一次。** 來源依序是 argv 的 `--platform <名稱>`、
   環境變數 `AXIOMATIC_PLATFORM`，最後才是預設值。判不出來或名字不合法一律退回
   預設（fail-safe：一個拼錯的名字不該讓行程去寫一組沒有人讀的狀態檔，那個症狀
   是「指令送出去了卻什麼都沒發生」，而且沒有地方會講）。
2. **每個平台的狀態住在自己的目錄**：`state/<平台>/<平台>.<原檔名>`。
   兩層都帶著平台名字是刻意的重複——目錄保證兩個行程不會寫到同一個檔，檔名保證
   一個被複製到別處的檔案仍然說得出自己屬於誰。`.gitignore` 只要一條 `state/`
   就蓋住所有平台、所有狀態檔，**連原子寫入留下的 `.tmp` 一起蓋到**；逐檔列舉的
   寫法則是本 repo 反覆記錄過的那種「兩份平行清單，總有一份會被遺忘」。
3. **憑證檔的命名是約定：`<平台>_bot_token.md`。** 既有的
   `discord_bot_token.md` 與 `telegram_bot_token.md` 本來就是這個形狀，所以啟動
   器不必 import 任何一個 transport 就問得出「這個平台填了憑證沒有」。**沒填憑證
   的平台是「不存在」，不是「壞掉」**：啟動器不會為它起行程，也不會每次都抱怨。
   注意那一行 `f"{platform}_bot_token.md"` 對
   `test_gitignore_coverage` 的字面值抽取器是隱形的——這是安全的，因為每個平台
   的 transport 模組裡都有一行真正的字面值常數（`TELEGRAM_TOKEN_FILE` 等），
   分類仍然由那一行負責。新增平台時**那一行不能省**。

## 這個模組刻意不做的事

它**不 import** `discord_bot`（那會是循環）、不 import 任何 `webrunner_*`，也不讀
設定檔——`enabled_platforms()` 吃的是呼叫端已經載好的設定 dict。它只有標準函式庫。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 預設平台。也是「沒有 `platforms.<名稱>.enabled` 這一格時」唯一預設開著的那一個
# ——其餘平台預設全部關著（見 `_bot_config._DEFAULT_PLATFORMS`）。
DEFAULT_PLATFORM = "discord"

# 子行程怎麼知道自己是誰。兩條路都留著：argv 給人在主控台上直接下，環境變數給
# 啟動器 spawn 時帶下去（有些啟動路徑會重組 argv，環境變數不會被重組掉）。
PLATFORM_ENV_VAR = "AXIOMATIC_PLATFORM"
PLATFORM_FLAG = "--platform"

# 所有平台狀態的根目錄。**字面值寫在這裡**，所以
# `test_gitignore_coverage._root_literals` 抽得到它、逼人分類它。
STATE_ROOT = PROJECT_ROOT / "state"

# 平台名字的形狀。收窄成小寫英數是為了它會直接變成路徑片段與檔名前綴：`..`、
# 路徑分隔符號、空白全部進不來，所以「使用者設定檔裡的一個字串」不可能變成
# 「寫到 repo 外面某個地方」。
_VALID_NAME = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")


def normalise_platform(raw) -> str:
    """把一個平台名字正規化；不合法就回 `DEFAULT_PLATFORM`。"""
    text = ("" if raw is None else str(raw)).strip().lower()
    return text if _VALID_NAME.match(text) else DEFAULT_PLATFORM


def platform_from_argv(argv=None) -> str | None:
    """argv 裡的 `--platform <名稱>` 或 `--platform=<名稱>`；沒有就回 `None`。

    自己走一遍而不是用 `argparse`：這支被 `discord_bot.py` 在**模組層**呼叫，
    那裡的 argv 還帶著別人的旗標（單圖伺服器旗標等），而 `argparse` 對不認得的
    旗標會直接 `SystemExit`——在 import 期把整支 bot 殺掉。
    """
    items = list(sys.argv[1:] if argv is None else argv)
    for index, item in enumerate(items):
        if item == PLATFORM_FLAG and index + 1 < len(items):
            return items[index + 1]
        if item.startswith(PLATFORM_FLAG + "="):
            return item.split("=", 1)[1]
    return None


def active_platform(argv=None, env=None) -> str:
    """這個行程服務哪一個平台。argv → 環境變數 → 預設值。"""
    environ = os.environ if env is None else env
    raw = platform_from_argv(argv)
    if raw is None:
        raw = environ.get(PLATFORM_ENV_VAR)
    return normalise_platform(raw)


def state_dir(platform: str | None = None) -> Path:
    """`state/<平台>/`。**不保證存在**，要寫東西之前先呼叫 `ensure_state_dir()`。"""
    return STATE_ROOT / normalise_platform(platform or active_platform())


def ensure_state_dir(platform: str | None = None) -> Path:
    """建出 `state/<平台>/` 並回傳它。**永不 raise。**

    建不出來（唯讀磁碟、權限）時仍然回那個路徑：真正要寫檔的那一刻本來就各自有
    錯誤處理，而在這裡炸掉會讓整支行程起不來。留一行紀錄，因為「狀態檔一個都沒
    落地」從外面看不出來。

    **呼叫時機是行程的 `main()`，不是 import。** `platform_file()` 刻意是純路徑
    計算：import 一個模組不該在版本庫裡建目錄。測試會 import 這些模組，而本專案
    有一道夾具在守「測試不得寫進 repo」——在 import 期 mkdir 會讓那道守門對一堆
    無辜的測試開火，而會亂叫的守門會被人關掉。
    """
    path = state_dir(platform)
    if path.is_dir():
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"platform state dir is not writable ({type(error).__name__})",
              file=sys.stderr)
    return path


def platform_file(base: Path, *, platform: str | None = None) -> Path:
    """`<repo>/x.json` → `<repo>/state/<平台>/<平台>.x.json`。

    引數刻意收一個**完整的 base 路徑**而不是一個裸檔名：呼叫端寫的仍然是
    `platform_file(PROJECT_ROOT / "events.ndjson")`，所以那個字面檔名留在原地，
    `test_gitignore_coverage` 的抽取器看得到、`test_atomic_writes` 的常數→檔名
    對應也解得出來。換成裸字串的話那兩道守門會同時變成空轉的綠燈。

    **純函式，不碰磁碟。** 目錄由 `ensure_state_dir()` 在行程的 `main()` 裡建一次，
    理由寫在那一支。
    """
    name = normalise_platform(platform or active_platform())
    stem = Path(base).name
    # 點開頭的檔名（鎖檔）把平台名放在點**後面**，否則會長出 `discord..x` 這種
    # 兩個點的名字——能用，但一看就像壞掉了，而看起來壞掉的東西會被人「修」。
    leaf = f".{name}.{stem[1:]}" if stem.startswith(".") else f"{name}.{stem}"
    return state_dir(name) / leaf


def token_file(platform: str | None = None) -> Path:
    """`<平台>_bot_token.md`。約定命名，見模組 docstring 第 3 點。"""
    return PROJECT_ROOT / f"{normalise_platform(platform)}_bot_token.md"


def has_credentials(platform: str | None = None) -> bool:
    """這個平台的憑證檔存在而且不是空的。讀不到一律當成「沒設定」。"""
    try:
        return bool(token_file(platform).read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def platform_enabled(config: dict | None, platform: str) -> bool:
    """設定檔說這個平台開著嗎。

    預設平台**沒有那一格時視為開著**（它的設定散在頂層：頻道 id、擁有者 id、
    憑證檔），其餘平台預設關著。兩邊都可以用 `platforms.<名稱>.enabled` 明確關掉
    ——「每個平台都要能各自關掉」包含預設的那一個。
    """
    name = normalise_platform(platform)
    section = ((config or {}).get("platforms") or {}).get(name)
    if isinstance(section, dict) and "enabled" in section:
        return bool(section["enabled"])
    return name == DEFAULT_PLATFORM


def platform_survey(config: dict | None, *, require_credentials: bool = True
                    ) -> list[tuple[str, bool, str]]:
    """每個設定檔提到的平台 →（名稱, 會不會起行程, 原因）。預設平台永遠排第一。

    **沒起來的一定要說得出原因。** 「設定開著卻沒起來」與「根本沒設定」從外面看
    一模一樣，而這是本 repo 一再點名的形狀；`start_platforms.py --list` 與
    `install_autostart.py --status` 印的就是這裡的原因欄。

    `require_credentials=False` 只給「只想知道設定檔開了哪些」的呼叫端。啟動器
    用預設值：**憑證沒填的平台是缺席，不是失敗**，因為那是絕大多數人的常態
    （沒有人會同時接四個平台），而每次啟動都抱怨一次就是下一個把記錄檔洗掉的
    雜訊源。
    """
    section = (config or {}).get("platforms") or {}
    names = [DEFAULT_PLATFORM]
    names += sorted(normalise_platform(n) for n in section
                    if normalise_platform(n) != DEFAULT_PLATFORM)
    rows: list[tuple[str, bool, str]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if not platform_enabled(config, name):
            rows.append((name, False,
                         f"設定檔裡關著（platforms.{name}.enabled）"))
        elif require_credentials and not has_credentials(name):
            rows.append((name, False,
                         f"憑證檔 {name}_bot_token.md 不存在或是空的"))
        else:
            rows.append((name, True, "會啟動"))
    return rows


def enabled_platforms(config: dict | None, *, require_credentials: bool = True
                      ) -> list[str]:
    """要起行程的平台清單。`platform_survey()` 的「會啟動」那一半。"""
    return [name for name, ok, _why
            in platform_survey(config, require_credentials=require_credentials)
            if ok]


# ---------------------------------------------------------------------------
# 登入自動啟動的排程工作名稱
# ---------------------------------------------------------------------------
# **兩邊共用同一份計算，不是兩份手抄的名單。** `install_autostart.py` 照這份去註冊，
# `_process_control.autostart_recovery_status()`（`/sys doctor` 用的那支）照同一份去
# 查「該註冊的都註冊了嗎」。以前那是兩份各自手打的常數，而改一邊不改另一邊的症狀是
# **零**：doctor 會從此每次都說「自動復原鏈路缺一角」（一個永遠為真的警告，看的人
# 第三次就會忽略它），或者反過來，一支工作真的沒註冊也不會有人說話。
AUTOSTART_TASK_FOLDER = "\\Axiomatic"
AUTOSTART_BATCH_TASK = AUTOSTART_TASK_FOLDER + "\\Batch"


def autostart_bot_task(platform: str) -> str:
    """一個平台一筆工作。**名字裡一定要帶平台名**：共用一個名字的話，第二次
    `schtasks /Create /F` 會把第一個平台那筆覆寫掉，而症狀是「裝好了，但只有最後
    一個平台會自己起來」——沒有錯誤訊息，要等下一次重開機才看得到。"""
    return f"{AUTOSTART_TASK_FOLDER}\\Bot-{normalise_platform(platform)}"


def autostart_task_names(config: dict | None) -> tuple[str, ...]:
    """登入自動啟動**應該**存在的工作：開著的平台各一筆，加上批次那一筆。"""
    return tuple(autostart_bot_task(name)
                 for name in enabled_platforms(config)) + (AUTOSTART_BATCH_TASK,)
