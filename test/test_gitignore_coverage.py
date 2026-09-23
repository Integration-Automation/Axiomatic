"""Repo root 的每個檔案／目錄常數都必須被分類：不是專案資產，就是被 gitignore。

這條規則本來只活在 code review 裡，於是漏了一票：`dorossi_events.ndjson`（內含
使用者 id）、`dorossi_queue{,_failed}.ndjson`、`generate_history.ndjson`、
`favorites.json`、`batch_label.txt`、`webrunner.pause`，以及最糟的
`.chrome_profile_verify/`——那是**登入 profile 的複本**，`.chrome_profile/` 與
`.chrome_profile_snap/` 早就因為「含登入 cookie」被 ignore，只有它漏網。

失敗形態是**安靜的**：檔案照樣被寫出來，只是某天一個手滑的 `git add` 就把本機
資料（甚至 cookie）送進版本庫，而 `git status` 從頭到尾看起來很正常。
`CLAUDE.md` 的「逐檔 stage」規則降低了機率，沒有消除它。

所以這支測試用 AST 掃出所有 `PROJECT_ROOT / "<literal>"`（含 `_PROJECT_ROOT`，
以及根目錄啟動器的 `REPO_ROOT`），要求每一個 literal 都出現在下面**兩個名單其中
之一**：新增一個 repo root 檔案而沒分類 → 測試紅，這是刻意的 fail-closed。

憑證檔（`auth.md`、`discord_bot_token.md`）與使用者自己的佇列／提示詞內容在這裡
是**執行期資料**：repo 只帶 `*.example.*` 範本，實際檔案一律 gitignore。把它們搬
回資產名單等於讓一次手滑的 `git add` 把 token 推上去。
"""
import ast
import os
import subprocess  # nosec B404
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent
# 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄）。搬家前它們在
# 套件的 glob 裡，所以下面的掃描把這個目錄一起列進來，範圍照舊。
TEST_ROOT = Path(__file__).resolve().parent

# 掃描對象：整個專案（套件內 ＋ repo root 的入口腳本），而且**每個模組都用同一組
# 名字**。
#
# 2026-09-10 之前這裡分成兩半，兩半各有一個真的缺口：
#
# * **名字集合按「模組住在哪」決定，而不是按「模組實際用哪個名字」。** 套件模組
#   只認 (`_`)`PROJECT_ROOT`、repo root 只認 `REPO_ROOT`。而 `gen_command_docs.py`
#   是**套件模組**、卻寫 `REPO_ROOT / "commands"`——那一行對這道守門完全隱形，於是
#   `commands/`（產生出來、要進版本庫的指令文件）一直沒有被分類，而這支測試的全部
#   意義就是「repo root 的每個名字都要選邊站」。
# * **root 那一半是寫死的四元組，套件那一半卻是 glob。** 同一個檔案裡兩支守門對
#   「範圍」給出不同答案——那個差異本身就是訊號（§8.8 的第八個實例，而且它正是靠
#   這個訊號找到的）。
#
# 用聯集會不會誤抓？在這個 repo 裡三個名字都指向同一個地方，而且誤抓的方向是安全
# 的：多要求分類一個名字，代價是加一行；漏掉一個的代價是把本機資料送進版本庫。
_ROOT_NAMES = frozenset({"PROJECT_ROOT", "_PROJECT_ROOT", "REPO_ROOT"})

# 舊的兩半留著，只給下面的範圍對照組用——它要能重現「縮回去會漏掉誰」。
_LEGACY_PKG_ROOT_NAMES = frozenset({"PROJECT_ROOT", "_PROJECT_ROOT"})
_LEGACY_TOP_SCRIPTS = ("start_discord_bot.py", "start_webrunner.py",
                       "run_batch.py", "install_autostart.py")


def _scanned_modules(pkg_root=None, repo_root=None) -> tuple[Path, ...]:
    """所有產品端模組：套件內 ＋ repo root 的入口腳本。

    兩個目錄可以換掉，是為了讓「這是 glob，不是寫死的清單」這件事**可以被測**：
    repo root 今天剛好就是那四支，所以在真實資料上兩者分不出來（變異實測，見
    `test_the_repo_root_half_is_a_glob_not_a_hardcoded_list`）。
    """
    pkg_root = PKG_ROOT if pkg_root is None else pkg_root
    repo_root = REPO_ROOT if repo_root is None else repo_root
    # `test/` 照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在套件裡、在這個
    # 範圍內，搬家之後照舊。從 `repo_root` 推，合成語料換掉 root 時它跟著換。
    package = [p for p in (*sorted(pkg_root.glob("*.py")),
                           *sorted((repo_root / "test").glob("*.py")))
               if not p.name.startswith(("test_", "_test_"))]
    root = [p for p in sorted(repo_root.glob("*.py"))
            if not p.name.startswith(("test_", "_test_"))]
    return tuple(package + root)

# 專案資產：**應該**進版本庫，因此不得被 gitignore 掃到。
_TRACKED_ASSETS = frozenset({
    "axiomatic",            # 套件本身
    "start_webrunner.py",    # run_batch.py 轉呼叫的啟動器
    # 相容用的舊進入點，轉呼叫 `start_webrunner.py`。存在於磁碟、也在版本庫裡，
    # 但 2026-09-18 之前**從來沒被分類過**——引用它的只有 `test_language._sources()`，
    # 而這支的掃描範圍刻意排除 `test_*`，所以那個引用看不到。`commands` 那一筆是
    # 同一個形狀（寫在套件模組裡卻用 `REPO_ROOT`），這一筆則是寫在測試裡。
    # 由 `audit_simplified_chars.py` 沿用同一份掃描範圍時翻出來。
    "run_batch.py",
    "install_autostart.py",  # 登入自動啟動的註冊/移除工具（開發者手動執行）
    "start_discord_bot.py",  # bot 監督啟動器
    "batch_config.json",
    "bot_prompts",
    # `gen_command_docs.py` 產生的指令參考（DoD #2：由指令樹產生，不得手改）。
    # 產生出來的東西照樣要進版本庫——2026-09-10 之前它連被分類的機會都沒有，
    # 因為那一行寫在套件模組裡卻用 `REPO_ROOT`，掃描的名字集合看不到它。
    "commands",
    # Sphinx 文件（`docs/conf.py` ＋ `.md`），追蹤。**第三次同一個形狀**：
    # `commands` 與 `run_batch.py` 都是「引用它的地方不在掃描範圍內」才一直沒被
    # 分類，這一筆則是 2026-09-20 把 `audit_simplified_chars.sources()` 的 repo root
    # 那一半從 `start_*.py` 放寬成 `*.py` ＋ `docs/*.py` 時當場翻出來的——在那之前
    # 目錄名只以 `REPO_ROOT / folder` 這種變數形式出現，抽取器只認字面值，看不到。
    "docs",
    "requirements.txt",
    "templates",             # 使用者手放的提示詞範本（bot 只讀）
    # 測試目錄（2026-09-22 從 `axiomatic/` 搬出來）。維護工具找測試檔時寫
    # `PROJECT_ROOT / "test"`（`mutation_harness`、
    # `audit_simplified_chars`），所以它跟 `docs` 一樣要選邊站：追蹤。
    "test",
})

# 執行期產物／本機資料：**必須**被 gitignore 蓋到。
#
# 前四組是**使用者自己的東西**，不是專案資產：憑證、bot 設定（頻道／擁有者 ID、
# 主機路徑白名單）、presence 對應（帳號 id 與本機應用程式清單）、四條佇列與它們的
# fallback 提示詞。repo 只帶 `*.example.*` 範本；真檔一律不追蹤，因為一次手滑的
# `git add` 就等於把 token 或私人內容推上去，而那是不可逆的。
_IGNORED_RUNTIME = frozenset({
    "auth.md",                       # 出圖服務帳密（範本：auth.example.md）
    "discord_bot_token.md",          # Discord bot token（範本同上）
    "bot_config.json",               # 頻道／擁有者 ID、啟動白名單（範本：.example.json）
    "presence_games.json",           # 本機遊戲清單
    "presence_music.json",           # 音樂偵測規則
    "presence_rpc.json",             # Rich Presence 的 application id
    "character1.md",                 # 角色1 fallback（使用者內容）
    "character2.md",                 # 角色2 fallback
    "default_prompt.md",             # 主提示詞範本
    "prompt.md",                     # 主提示詞 fallback
    "undesired.md",                  # 負面提示詞 fallback
    "todo_prompt.md",                # 四條佇列，bot 寫、webrunner 讀
    "todo_character1.md",
    "todo_character2.md",
    "todo_undesired.md",
    ".backup",                       # /sys undo 的備份堆疊
    ".chrome_profile",               # 登入 session（含 cookie）
    ".chrome_profile_snap",          # 同上，批次用的複本
    ".chrome_profile_verify",        # 同上，驗證腳本用的複本
    ".discord_bot.lock",             # bot 本體實例鎖
    ".discord_bot_supervisor.lock",  # bot 監督者實例鎖
    ".webrunner_supervisor.lock",    # 批次監督者實例鎖
    ".venv",
    "audit.ndjson",
    "batch_label.txt",
    "chrome_slot.lock",
    "chrome_slot.steal.lock",  # 搶佔臨界區的第二把鎖
    "chromedriver.log",
    "chromedriver.prev.log",  # 上一個 driver 工作階段（崩潰後要看的那一份）
    "discord_bot.log",             # 監督者 tee 下來的 bot 主控台輸出
    "webrunner.prev.log",           # 上一輪的 log（崩潰後要看的那一份）
    "dom_request.json",
    "dorossi_events.ndjson",         # 含使用者 id
    "dorossi_models.json",           # 每日檢查讀回來的後端模型目錄（本機執行期資料）
    "dorossi_queue.ndjson",
    "dorossi_queue_failed.ndjson",
    "dorossi_session.json",
    "dorossi_usage.ndjson",
    "dorossi_workspace",
    "events.ndjson",
    "favorites.json",                # 使用者的收藏，本機資料
    "generate_history.ndjson",
    "macros",
    "ocr_tessdata",
    "output",
    "recent_image_msgs.json",
    "scheduled_run.json",            # `/run in`／`/run at` 的延後啟動，重啟後還原用
    "network_resume.json",           # 斷網停下來的批次，網路回來就接續（重啟後還原用）
    "schedules.json",
    "single_image_request.json",
    "webrunner.log",
    "webrunner.pause",
    "webrunner.pid",
    "webrunner_progress.json",
    "window_layouts",
})

# 原子寫入會在**同目錄**留下 `<name>.tmp`（`_atomic_write_text` 等），行程被砍掉
# 就會殘留。母檔要 ignore 的，`.tmp` 也要。
#
# **這份清單是推導出來的，不是手打的。** 2026-09-07 之前它是手打的，然後就發生了
# 意料中的事：`test_atomic_writes._CROSS_PROCESS_CONSTANTS`（真正列出「哪些檔案走
# 原子寫入」的地方）陸續多了 `WEBRUNNER_PID_FILE` / `DOM_REQUEST_FILE` /
# `RECENT_IMAGE_MSGS_FILE`，而沒有人回頭同步這一份——`git check-ignore` 實測那三個
# `.tmp` 全是 NOT IGNORED。兩份平行清單只要有一份會被忘記，這條規則就是壞的。
# 現在改成從那份常數集合 ＋ AST 解出來的字面檔名推導，新增一個原子寫入就自動要求
# 它的 `.tmp`。
_ATOMIC_TMP_EXTRA = frozenset({
    # 不是由 `_CROSS_PROCESS_CONSTANTS` 管、但確實會留下 `.tmp` 的。每筆寫理由。
    "dorossi_events.ndjson",      # `_dorossi_event` 直接用 `_atomic_write_text`
    "dorossi_queue.ndjson",       # `_dorossi_queue_write` 整檔重寫
    "dorossi_queue_failed.ndjson",  # 同上
    "dorossi_session.json",       # `_dorossi_save_state`
    "dorossi_usage.ndjson",       # `_dorossi_trim_usage_file`
    "webrunner_progress.json",    # `_run_progress._atomic_write`
})


def _atomic_tmp_parents() -> frozenset:
    """`test_atomic_writes` 認定要原子寫入的那些常數，解成 repo root 的檔名。

    常數名 → 字面檔名的對應由 AST 從各模組的 `X = <root> / "literal"` 取得，
    所以改了檔名這裡會自動跟上；解不出來的常數會讓下面那支自我檢查變紅。
    """
    import test_atomic_writes as ta

    wanted = set(ta._CROSS_PROCESS_CONSTANTS)
    resolved: dict[str, str] = {}
    for path in _scanned_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in wanted):
                continue
            value = node.value
            if (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
                    and isinstance(value.right, ast.Constant)
                    and isinstance(value.right.value, str)):
                resolved.setdefault(node.targets[0].id, value.right.value)
    return frozenset(resolved.values()) | _ATOMIC_TMP_EXTRA


def test_every_atomic_write_target_resolves_to_a_filename():
    """守門的自我檢查：解不出檔名的常數會讓 `.tmp` 那支變成空轉的綠燈。"""
    import test_atomic_writes as ta

    parents = _atomic_tmp_parents()
    assert len(parents) >= 15, f"只解出 {len(parents)} 個檔名，推導可能壞了"
    # 每一個 `_CROSS_PROCESS_CONSTANTS` 都要解得出來，否則就是漏掉一個母檔。
    import ast as _ast
    resolved_names = set()
    for path in _scanned_modules():
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], _ast.Name)
                    and node.targets[0].id in ta._CROSS_PROCESS_CONSTANTS):
                resolved_names.add(node.targets[0].id)
    unresolved = sorted(set(ta._CROSS_PROCESS_CONSTANTS) - resolved_names)
    assert not unresolved, (
        f"這些原子寫入常數在套件裡找不到 `X = <root> / \"檔名\"` 的定義："
        f"{unresolved}。它們的 `.tmp` 因此沒有被這支守門涵蓋。")


def _root_literals(sources=None, names=None) -> dict[str, set[str]]:
    """AST 掃出所有 `<root> / "<literal>"`，回 {literal: {來源檔名}}。

    兩個引數只給範圍對照組用（預設在**呼叫時**才解析，不是寫死在 def 上）。
    """
    sources = _scanned_modules() if sources is None else sources
    names = _ROOT_NAMES if names is None else names
    found: dict[str, set[str]] = {}

    def scan(path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Div)
                    and isinstance(node.left, ast.Name)
                    and node.left.id in names
                    and isinstance(node.right, ast.Constant)
                    and isinstance(node.right.value, str)):
                found.setdefault(node.right.value, set()).add(path.name)

    for p in sources:
        if p.exists():
            scan(p)
    return found


def _git(*args: str) -> subprocess.CompletedProcess:
    # `encoding` 不能省：`text=True` 會用平台地區編碼解碼（本機 cp950），而 git
    # 吐的是 UTF-8。這裡讀的是檔案路徑，今天剛好都是 ASCII，但版本庫裡多一個非
    # ASCII 檔名就會讓這支守門測試爆在解碼上、而不是報出它真正要說的話。
    return subprocess.run(  # nosec B603 B607
        ["git", *args], cwd=str(REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)


def _require_git() -> None:
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git work tree")


def _ignored_set(candidates) -> set:
    """哪些候選路徑被 `.gitignore` 蓋到。

    目錄型 pattern（`macros/`）對不存在的路徑不會命中，所以每個候選同時試
    `<name>` 與 `<name>/probe`，任一命中就算蓋到。反方向（資產「不得被 ignore」）
    只試 `<name>`——`auth.md/probe` 會讓 `auth.md` 這個 pattern 誤命中。
    """
    probes = list(candidates) + [c + "/probe" for c in candidates]
    proc = _git("check-ignore", "--no-index", *probes)
    if proc.returncode not in (0, 1):  # 0=有命中 1=全沒命中
        pytest.skip("git check-ignore unavailable: " + proc.stderr.strip())
    hit = set()
    for raw in proc.stdout.splitlines():
        line = raw.strip().replace("\\", "/")
        if not line:
            continue
        hit.add(line[:-len("/probe")] if line.endswith("/probe") else line)
    return hit


@pytest.fixture(scope="module")
def literals():
    return _root_literals()


def test_every_root_literal_is_classified(literals):
    """新增一個 repo root 檔案就必須選邊站——這是 fail-closed 的那一半。"""
    known = _TRACKED_ASSETS | _IGNORED_RUNTIME
    unclassified = sorted(set(literals) - known)
    assert not unclassified, (
        "這些 repo root 路徑沒有分類，請加進 test_gitignore_coverage.py 的 "
        "_TRACKED_ASSETS（專案資產）或 _IGNORED_RUNTIME（執行期產物，同時要"
        "寫進 .gitignore）："
        + str({n: sorted(literals[n]) for n in unclassified}))

    overlap = sorted(_TRACKED_ASSETS & _IGNORED_RUNTIME)
    assert not overlap, "同時被列成資產與執行期產物：" + str(overlap)


def test_runtime_artifacts_are_gitignored():
    """執行期產物必須真的被蓋到——漏掉的那次差點把登入 cookie 送進版本庫。"""
    _require_git()
    names = sorted(_IGNORED_RUNTIME)
    missing = sorted(set(names) - _ignored_set(names))
    assert not missing, "這些執行期產物沒有被 .gitignore 蓋到：" + str(missing)


def test_atomic_write_temps_are_gitignored():
    """原子寫入殘留的 `<name>.tmp` 與母檔同命運。"""
    _require_git()
    names = sorted(n + ".tmp" for n in _atomic_tmp_parents())
    missing = sorted(set(names) - _ignored_set(names))
    assert not missing, "這些原子寫入殘留檔沒有被 .gitignore 蓋到：" + str(missing)


def test_tracked_assets_are_not_gitignored():
    """反方向：資產被 ignore 掉，fresh clone 會少檔案而且沒人會發現。"""
    _require_git()
    names = sorted(_TRACKED_ASSETS)
    proc = _git("check-ignore", "--no-index", *names)
    if proc.returncode not in (0, 1):
        pytest.skip("git check-ignore unavailable: " + proc.stderr.strip())
    hit = sorted(x.strip().replace("\\", "/")
                 for x in proc.stdout.splitlines() if x.strip())
    assert not hit, "這些專案資產被 .gitignore 蓋到了：" + str(hit)


def test_existing_assets_are_actually_tracked():
    """存在於磁碟上的資產必須真的在版本庫裡（漏 `git add` 也是安靜的失敗）。"""
    _require_git()
    untracked = []
    for name in sorted(_TRACKED_ASSETS):
        if not (REPO_ROOT / name).exists():
            continue  # 例如 templates/ 由使用者自行建立
        if not _git("ls-files", "--", name).stdout.strip():
            untracked.append(name)
    assert not untracked, "這些資產存在但沒被追蹤：" + str(untracked)


# ---------------------------------------------------------------------------
# 範圍本身的釘樁（§8.8：規則的文字沒提到模組，掃描就不能只看其中幾個）
# ---------------------------------------------------------------------------

_SCAN_FLOOR = 25


def _assert_scan_floor(sources) -> None:
    assert len(sources) >= _SCAN_FLOOR, (
        f"掃描集合只剩 {len(sources)} 個模組（下限 {_SCAN_FLOOR}）——列舉器壞了，"
        "或是範圍被縮回手寫清單。掃到 0 個時上面每一支都會過，那是最安靜的失效。")


def test_the_scan_covers_the_whole_project():
    _assert_scan_floor(_scanned_modules())


def test_the_floor_fires_when_the_enumerator_comes_back_empty():
    """控制組：空的列舉必須紅在下限那一句上，不是別的地方。"""
    with pytest.raises(AssertionError, match="掃描集合只剩"):
        _assert_scan_floor(())


def test_every_repo_root_script_is_scanned():
    """repo root 的入口腳本不能靠手寫清單維護。

    `run_batch.py` 這一類東西寫的是 `REPO_ROOT / "<執行期檔案>"`，而漏掉一個的
    後果正是這支測試存在的理由：檔案被寫出來、`git status` 看起來正常、一次
    `git add` 就把本機資料送進版本庫。
    """
    scanned = {p.name for p in _scanned_modules()}
    on_disk = {p.name for p in REPO_ROOT.glob("*.py")
               if not p.name.startswith(("test_", "_test_"))}
    assert on_disk - scanned == set(), (
        f"repo root 有腳本沒被掃到：{sorted(on_disk - scanned)}")
    assert on_disk, "repo root 一支 .py 都沒有？列舉器壞了"


_ROOT_PRESERVING_METHODS = frozenset({"resolve", "expanduser", "absolute"})
_ROOT_PRESERVING_ATTRS = frozenset({"parent", "parents"})


def _root_derived(node) -> bool:
    """這個運算式指的是不是「從 root 常數推出來的路徑」（比 `_root_literals` 寬）。"""
    if isinstance(node, ast.Name):
        return node.id in _ROOT_NAMES
    if isinstance(node, ast.Call):
        func = node.func
        return (isinstance(func, ast.Attribute)
                and func.attr in _ROOT_PRESERVING_METHODS
                and _root_derived(func.value))
    if isinstance(node, ast.Attribute):
        return (node.attr in _ROOT_PRESERVING_ATTRS
                and _root_derived(node.value))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _root_derived(node.left)
    return False


def _first_literal_segment(node):
    """鏈式接合最左邊那一段字面值；左邊不是裸 root 名字時回 None。"""
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if isinstance(node.left, ast.Name) and node.left.id in _ROOT_NAMES:
            return (node.right.value
                    if isinstance(node.right, ast.Constant)
                    and isinstance(node.right.value, str) else None)
        node = node.left
    return None


def _chained_root_joins(sources=None) -> list:
    """`<推導自 root 的東西> / "<字面值>"`，但左邊**不是**裸 root 名字的站點。

    回 `[(檔名, 行號, 運算式, 最左邊那一段字面值或 None)]`。
    """
    sources = _scanned_modules() if sources is None else sources
    out = []
    for path in sources:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp)
                    and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant)
                    and isinstance(node.right.value, str)):
                continue
            if isinstance(node.left, ast.Name) and node.left.id in _ROOT_NAMES:
                continue                  # `_root_literals` 已經看得見
            if not _root_derived(node.left):
                continue                  # 跟 root 常數無關，不歸這支管
            out.append((path.name, node.lineno, ast.unparse(node)[:70],
                        _first_literal_segment(node.left)))
    return sorted(set(out))


def _unanchored_chained(chained, known) -> list:
    """哪些鏈式站點錨不到一個已分類的第一段。

    抽成 helper 是因為**真實語料今天一筆都沒有**，所以把 `row[3] is None` 那半條
    件放水掉，整組照樣全綠（2026-09-11 變異實測 SURVIVED）。判斷邏輯自己也要有
    合成對照組，不只抽取器要有。
    """
    return [row for row in chained if row[3] is None or row[3] not in known]


def test_every_chained_root_path_is_anchored_by_a_classified_first_segment():
    """`_root_literals` 只認 `<裸 root 名字> / "<字面值>"`，所以**鏈式接合看不見**。

    這正是接合守門 2026-09-11 之前的同一種盲點（`isinstance(node.left,
    ast.Name)`）。差別是：**這一支守門的職責是「repo root 的執行期產物」**，而鏈式
    路徑落在哪，是由它**最左邊那一段**決定的——那一段本來就會被 `_root_literals`
    抽到並分類。所以漏掉鏈式接合今天沒有後果……**只要那個前提成立**。

    這支測試就是那個前提的檢查。實測 2026-09-11：12 個鏈式站點，第一段全部是
    `axiomatic` 或 `.venv`，兩個都在抽到的字面值裡。

    會變紅的那一天，是有人寫出 `(PROJECT_ROOT).resolve() / "cache.tmp"` 或
    `PROJECT_ROOT.parent / "x"`——那種**沒有第一段字面值**的形狀，`_root_literals`
    完全看不見它，而它可能真的落在 repo root。那時候要做的是把 `_root_literals`
    一起放寬，不是把這一支關掉。
    """
    chained = _chained_root_joins()
    # 正面對照：語料裡真的有這種形狀，否則下面等於沒在檢查（§8.8(A3)）。
    assert len(chained) >= 8, (
        f"只抽到 {len(chained)} 個鏈式站點（實測 2026-09-11 是 12 個）——"
        "抽取器或掃描範圍壞了，下面那句斷言會空轉通過。")
    known = set(_root_literals())
    assert known, "`_root_literals` 回空集合，下面的比對沒有意義"
    unanchored = _unanchored_chained(chained, known)
    assert not unanchored, (
        "這些鏈式路徑錨不到一個已分類的第一段：%s。`_root_literals` 看不見它們"
        "（它只認裸 root 名字當左運算元），而第一段又沒被別處抽到——所以這條路徑"
        "落在哪、有沒有被 gitignore 分類過，**沒有任何東西在看**。修法是放寬 "
        "`_root_literals`，不是放寬這一支。" % (unanchored,))


def test_the_chained_join_extractor_actually_bites():
    """上一支的合成對照組：真實語料今天是乾淨的，所以它**刪掉也會綠**。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad_module.py"
        bad.write_text(
            "from pathlib import Path\n"
            "PROJECT_ROOT = Path('.')\n"
            "A = PROJECT_ROOT.resolve() / 'cache.tmp'\n"      # 沒有第一段字面值
            "B = PROJECT_ROOT / 'axiomatic' / 'x.py'\n",     # 第一段 = axiomatic
            encoding="utf-8")
        rows = _chained_root_joins((bad,))
    got = {(r[2], r[3]) for r in rows}
    assert got == {("PROJECT_ROOT.resolve() / 'cache.tmp'", None),
                   ("PROJECT_ROOT / 'axiomatic' / 'x.py'", "axiomatic")}, got
    # 判斷邏輯本身也要對照，不只抽取器。三種輸入各問一次：
    known = {"axiomatic"}
    flagged = _unanchored_chained(rows, known)
    assert [r[2] for r in flagged] == ["PROJECT_ROOT.resolve() / 'cache.tmp'"], (
        "錨不到第一段的那一筆沒有被標出來（`row[3] is None` 那半條件可能被放水）："
        f"{flagged}")
    # 第一段抽到了、但**沒有被分類**，也算錨不到。
    assert len(_unanchored_chained(rows, set())) == 2, _unanchored_chained(rows, set())
    # 反向：兩筆都錨得到時必須回空，否則它就是一支對所有鏈式路徑開火的守門。
    assert _unanchored_chained(
        [r for r in rows if r[3] is not None], known) == []


def test_the_name_set_does_not_depend_on_where_the_module_lives():
    """套件模組一樣可能用 `REPO_ROOT`——這是 2026-09-10 真的漏掉的那一個。

    舊寫法把名字集合綁在「模組住在哪」：套件內只認 `PROJECT_ROOT`。
    `gen_command_docs.py` 是套件模組卻寫 `REPO_ROOT / "commands"`，於是那一行
    隱形，`commands/` 一直沒有被分類。這支同時釘住兩個方向：現在看得到，而且
    **用舊的名字集合就看不到**——後者才證明這支測試在說一件真的事。
    """
    wide = _root_literals()
    assert "commands" in wide, (
        "`gen_command_docs.py` 的 `REPO_ROOT / \"commands\"` 又掃不到了")
    assert "gen_command_docs.py" in wide["commands"]

    narrow = _root_literals(names=_LEGACY_PKG_ROOT_NAMES)
    assert "commands" not in narrow, (
        "用舊的名字集合竟然也掃得到——那這支測試證明不了任何事，"
        "去確認 `_LEGACY_PKG_ROOT_NAMES` 還是不是舊的那一組。")


def test_the_scope_pin_reproduces_what_the_old_hardcoded_list_missed():
    """控制組：把來源縮回舊的四元組 ＋ 套件 glob，必須少掉東西。

    範圍釘樁要能重現「縮回去會漏掉誰」，否則它只是在陳述現況。
    """
    legacy_sources = [p for p in (*sorted(PKG_ROOT.glob("*.py")),
                                  *sorted(TEST_ROOT.glob("*.py")))
                      if not p.name.startswith(("test_", "_test_"))]
    legacy_sources += [REPO_ROOT / n for n in _LEGACY_TOP_SCRIPTS]
    old = _root_literals(sources=legacy_sources,
                         names=_LEGACY_PKG_ROOT_NAMES)
    new = _root_literals()
    assert set(new) - set(old), (
        "加寬範圍之後一個字面量都沒有多出來——範圍 pin 沒有在量任何東西。")


def test_the_repo_root_half_is_a_glob_not_a_hardcoded_list(tmp_path):
    """釘住「列舉是**算出來的**」，不只是「今天答案剛好對」。

    repo root 現在剛好有四支 `.py`，跟 2026-09-10 之前那份手寫四元組**逐字相同**
    ——所以把列舉換回那個四元組，上面每一支測試照樣全綠（變異實測，那是唯一一個
    存活下來的變異）。§8.8(A3)：真實資料乾淨時，寫死的範圍和算出來的範圍在輸出
    上一模一樣，唯一能分辨的辦法是餵它一個**清單裡不可能有的名字**。
    """
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "some_module.py").write_text("X = 1\n", encoding="utf-8")
    (root / "brand_new_entry_point.py").write_text("X = 1\n", encoding="utf-8")
    (root / "test_not_a_product_module.py").write_text("", encoding="utf-8")

    names = {p.name for p in _scanned_modules(pkg_root=pkg, repo_root=root)}
    assert "brand_new_entry_point.py" in names, (
        "repo root 新出現的腳本沒有被算進來——列舉是寫死的清單，不是 glob。"
        "新的入口腳本會因此完全不受這道守門保護。")
    assert "some_module.py" in names, "套件那一半也要跟著換得掉"
    assert "test_not_a_product_module.py" not in names, (
        "測試檔被算成產品端模組了")


# ---------------------------------------------------------------------------
# 已追蹤的檔案不得在模組層匯入未追蹤的專案模組
# ---------------------------------------------------------------------------
# 上面整支測試守的是「repo root 的每個檔案都要選邊站」。這一段守的是另一半：
# **提交的內容本身要自洽。**
#
# 失敗形態跟上面同一族，而且更安靜：把「這一輪改到的檔」逐個 `git add` 進去、
# 卻忘了一起加它新 import 的那個**新模組**。commit 會過，本機一切正常（檔案就在
# 磁碟上，import 找得到），但**新 clone 一 import 就死**。本機永遠測不出來，因為
# 本機那個檔一直都在。
#
# 逐檔 stage 那條規定（`CLAUDE.md` 的 Git 段，不准 `git add -A`，理由是 repo 刻意
# 追蹤明文憑證）本身是對的，副作用正好是讓這種漏掉更容易發生。而這個 repo 最近
# 一直在長新的被動共用模組（`_warn_dedup`、`_code_fingerprint`、
# `_browser_killguard` 都是），所以這不是一次性的風險。
#
# 只看**模組層**的 import：函式內部的 import 不會讓 clone 一載入就死。

# 已知、刻意還沒提交的共用模組。每一筆都要寫理由。
#
# **這份名單是 fail-open 的**（列進來就不檢查），所以下面有一支反向對帳：一旦某個
# 模組真的被提交了，它的條目就會變成永遠不再命中的字串，那支測試會紅並要求刪掉它。
# 目前**刻意是空的**（2026-09-12）：`_code_fingerprint.py` 與 `_warn_dedup.py` 已經
# 連同匯入它們的檔一起提交，所以那兩筆豁免過期了、已移除。空的名單是上面那支守門
# **最嚴格**的狀態，不是它失效——別因為「現在沒有例外」就把守門拿掉。
#
# 什麼時候該往這裡加一筆：一個**已追蹤**的檔在模組層 import 了一個**還沒提交**的
# 專案模組，而你確定那是暫時狀態（例如匯入行與新模組是同一輪加的，`HEAD` 兩邊都
# 還沒有，所以 clone HEAD 仍然自洽）。加的時候連理由一起寫，理由會被檢查非空。
_UNCOMMITTED_SHARED_MODULES: dict = {}


def _tracked_paths() -> set:
    """`git ls-files` 的結果（repo 相對、posix 斜線）。

    **沒有 git／不是工作樹時 `pytest.skip`，不是 `assert`。** 一份壓縮檔解開來的
    原始碼樹沒有 `.git`，而「這裡沒有版本庫」不是這幾支測試在守的東西——讓它們紅，
    等於在一個根本量不到的環境裡報告一個假的違規。
    """
    _require_git()
    done = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    if done.returncode != 0:
        pytest.skip(f"`git ls-files` 跑不起來：{done.stderr[-200:]}")
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _project_modules_on_disk() -> dict:
    """模組名 → 路徑。套件內、`test/` 與 repo root 的 `.py` 都算。"""
    out = {}
    for path in (sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py"))
                 + sorted(REPO_ROOT.glob("*.py"))):
        out.setdefault(path.stem, path)
    return out


def _module_level_imports(path: Path) -> set:
    """只收模組層 import 的頂層名字（`import x` / `from x import y` 兩種都收）。

    `from x import y` 的 `y` 也收：這個 repo 的慣例是
    `from _warn_dedup import warn_once`，只看 `node.module` 會漏掉另一半形狀，
    而漏掉的方式跟 `CLAUDE.md` 記著的 `_module_imports` 那個盲點一模一樣。

    **模組層的 `try` 區塊要鑽進去。** 這個 repo 的共用模組還有第二種慣例形狀——
    `try: from _x import y / except ImportError: from axiomatic._x import y`
    （`_external_apis`、`_bot_config`、`_batch_config`、`presence_probe` 都是），
    用途是同一個模組要能被裸名與套件路徑兩種方式匯入。只掃 `tree.body` 的話這一整
    類都看不到，而它們**照樣是模組層、照樣一 import 就執行**：兩邊都指向同一個
    專案模組，那個檔案沒被提交的話 fresh clone 兩條路都會死，只是死在 `except`
    裡面而已。2026-09-12 把三個載入器改成這個形狀時發現的。
    **只鑽 `try`，不鑽 `if`**：`if TYPE_CHECKING:` 底下的 import 執行期根本不跑，
    收進來就是誤報，而會亂叫的守門遲早被人關掉。
    """
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    todo = list(tree.body)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.Try):
            todo.extend(node.body)
            todo.extend(node.orelse)
            todo.extend(node.finalbody)
            for handler in node.handlers:
                todo.extend(handler.body)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
            names.update(alias.name for alias in node.names)
    return names


def _tracked_importing_untracked() -> list:
    """回 `(匯入者的相對路徑, [未追蹤的模組檔名, ...])`。"""
    tracked = _tracked_paths()
    on_disk = _project_modules_on_disk()
    rows = []
    for name, path in sorted(on_disk.items()):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel not in tracked:
            continue                    # 匯入者自己沒被追蹤 → 不是這一類問題
        missing = sorted(
            on_disk[imported].name
            for imported in _module_level_imports(path)
            if imported in on_disk
            and on_disk[imported].relative_to(REPO_ROOT).as_posix() not in tracked)
        if missing:
            rows.append((rel, missing))
    return rows


def test_no_tracked_module_imports_an_untracked_one():
    """已追蹤的檔案不得在模組層匯入未追蹤的專案模組（除非列了理由）。"""
    rows = _tracked_importing_untracked()
    # 正面對照組：抽取器壞掉回空集合時，「零筆違規」跟「全部乾淨」長得一模一樣。
    tracked = _tracked_paths()
    assert len(tracked) >= 60, (
        f"`git ls-files` 只回了 {len(tracked)} 筆——這不像這個 repo，"
        "下面等於沒在檢查。")
    offenders = [(rel, [m for m in missing
                        if m not in _UNCOMMITTED_SHARED_MODULES])
                 for rel, missing in rows]
    offenders = [(rel, missing) for rel, missing in offenders if missing]
    assert not offenders, (
        "這些**已追蹤**的檔案在模組層匯入了**未追蹤**的專案模組：\n"
        + "\n".join(f"  {rel} → {missing}" for rel, missing in offenders)
        + "\n提交時只 stage 匯入者、忘了被匯入的新模組，commit 會過、本機一切"
          "正常，但**新 clone 一 import 就死**——而且本機永遠測不出來。"
          "請把那些模組一起 `git add`，或（若是刻意的）連同理由登記進 "
          "`_UNCOMMITTED_SHARED_MODULES`。")


def _stale_exemptions(exemptions, tracked, by_filename) -> list:
    """哪幾筆例外已經不再指到「存在且未追蹤」的模組。

    **抽成純函式是為了讓合成資料問得到它。** 2026-09-10 用變異測試量到：把下面
    「是不是已經被追蹤了」那一條整個拿掉，全套 15 支照樣全綠——因為此刻真實資料裡
    根本沒有過期的條目，這句斷言在真實資料上是**問不出來**的。`CLAUDE.md` 對這個
    形狀已經寫了處方（「一份乾淨的清單會讓真實資料的斷言變得無法測試，刪掉它會
    變綠，所以比較本身要住在一支有自己合成對照的 helper 裡」），這裡照做。
    """
    stale = []
    for filename in exemptions:
        path = by_filename.get(filename)
        if path is None:
            stale.append(f"{filename}（磁碟上已經沒有這個模組）")
        elif path.relative_to(REPO_ROOT).as_posix() in tracked:
            stale.append(f"{filename}（已經被追蹤了）")
    return stale


def test_the_staleness_comparison_actually_reports_both_ways():
    """合成對照：上面那支 helper 的三種輸入各問一次。

    沒有這一支的話，`_stale_exemptions` 可以直接 `return []`，而全套照樣綠。
    """
    tracked = {"axiomatic/committed.py"}
    by_filename = {
        "committed.py": PKG_ROOT / "committed.py",     # 已經被追蹤 → 過期
        "still_new.py": PKG_ROOT / "still_new.py",     # 還沒被追蹤 → 正常
    }
    assert _stale_exemptions(["still_new.py"], tracked, by_filename) == []
    assert _stale_exemptions(["committed.py"], tracked, by_filename) == [
        "committed.py（已經被追蹤了）"]
    assert _stale_exemptions(["vanished.py"], tracked, by_filename) == [
        "vanished.py（磁碟上已經沒有這個模組）"]


def test_the_uncommitted_module_exemptions_are_not_stale():
    """名單是 fail-open 的，所以反向也要對帳。

    某個模組真的被提交之後，它的條目就變成一個永遠不再命中的字串——閘照樣跑、
    名單照樣在、每一支測試照樣綠，而那正是 `CLAUDE.md` 記著的
    `_OWNER_ONLY_SLASH` 那個形狀。
    """
    # **刻意沒有「名單不得為空」的下限。** 空名單是合法且理想的狀態（沒有任何
    # 豁免＝上面那支守門最嚴格）。空的時候這支確實會空轉，但 `_stale_exemptions`
    # 的牙齒住在 `test_the_staleness_comparison_actually_reports_both_ways` 那支
    # 合成對照裡，所以邏輯本身仍然被蓋到——這是本 repo 對「真實資料乾淨時如何保住
    # 覆蓋」的既有處方。
    for filename, reason in _UNCOMMITTED_SHARED_MODULES.items():
        assert reason.strip(), f"`{filename}` 的例外沒寫理由。"
    on_disk = _project_modules_on_disk()
    stale = _stale_exemptions(
        _UNCOMMITTED_SHARED_MODULES, _tracked_paths(),
        {path.name: path for path in on_disk.values()})
    assert not stale, (
        f"這些例外已經過期：{stale}。請把它們從 "
        "`_UNCOMMITTED_SHARED_MODULES` 刪掉——留著的話，下一個真的漏掉的模組"
        "會被同一筆例外默默放行。")


def test_the_import_extractor_sees_both_import_shapes():
    """合成對照：`import x` 與 `from x import y` 兩種形狀都要看得到。

    這個 repo 的共用模組慣例是 `from _warn_dedup import warn_once`，所以只讀
    `node.module` 會漏掉一半——那正是 `CLAUDE.md` 記著的 `_module_imports` 盲點
    （只讀 `ImportFrom.module`、不看 `names`）。而且函式**裡面**的 import 不算，
    因為那不會讓 clone 一載入就死。
    """
    import tempfile

    source = (
        "import alpha\n"
        "from beta import gamma\n"
        "from pkg.delta import epsilon\n"
        "def f():\n"
        "    import zeta\n")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(source, encoding="utf-8")
        names = _module_level_imports(probe)
    for expected in ("alpha", "beta", "gamma", "pkg", "epsilon"):
        assert expected in names, f"漏掉 {expected!r}：{sorted(names)}"
    assert "zeta" not in names, (
        "函式內部的 import 被當成模組層了——那會讓這道守門對一堆無害的延遲匯入"
        f"大呼小叫：{sorted(names)}")


def test_the_import_extractor_sees_the_dual_shape_try_fallback():
    """合成對照：`try: 裸名 / except ImportError: 套件路徑` 兩邊都要看得到。

    這是這個 repo 共用模組的第二種慣例形狀（`_external_apis` 一直是、三個設定檔
    載入器從 2026-09-12 起也是），而它**照樣是模組層、照樣一 import 就執行**。
    只掃 `tree.body` 的話整類都是隱形的：`_warn_dedup` 沒被提交，fresh clone 走
    `try` 死、走 `except` 也死，而守門會回報零筆違規。

    同一支順便釘住**不鑽 `if`**：`if TYPE_CHECKING:` 底下的 import 執行期不跑，
    收進來就是誤報。
    """
    import tempfile

    source = textwrap.dedent(
        """
        try:
            from _shared import helper
        except ImportError:
            from axiomatic._shared import helper
        try:
            import fast_thing
        except ImportError:
            fast_thing = None
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            import only_for_types
        """)
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.py"
        probe.write_text(source, encoding="utf-8")
        names = _module_level_imports(probe)
    for expected in ("_shared", "helper", "axiomatic", "fast_thing"):
        assert expected in names, (
            f"模組層 `try` 裡的 import 被漏掉了（{expected!r}）：{sorted(names)}。"
            "這個 repo 用這個形狀讓共用模組同時支援裸名與套件路徑匯入，漏掉它"
            "等於這道守門對整類共用模組失效。")
    assert "only_for_types" not in names, (
        "`if TYPE_CHECKING:` 底下的 import 被當成執行期匯入了——那在執行期根本"
        f"不跑，收進來就是誤報：{sorted(names)}")


def test_the_three_config_loaders_still_import_both_ways():
    """真實資料：三個設定檔載入器必須兩種匯入路徑都活著。

    2026-09-12 的實際故障——`_warn_dedup` 抽成共用模組時只寫了裸名匯入，
    `start_webrunner.py`（repo root，刻意走套件路徑）於是在 import 期
    `ModuleNotFoundError` 整支起不來。裸名那一側本機永遠是綠的，所以這裡量的是
    **套件路徑那一側**。
    """
    for name in ("_bot_config", "_batch_config", "presence_probe"):
        done = subprocess.run(  # nosec B603
            [sys.executable, "-c", f"from axiomatic import {name}"],
            cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert done.returncode == 0, (
            f"`from axiomatic import {name}` 死在 import 期——"
            f"`start_webrunner.py` 就是走這條路徑：{done.stderr[-600:]}")
