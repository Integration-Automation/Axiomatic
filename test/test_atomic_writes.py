"""跨行程檔案一律原子寫入（`CLAUDE.md` 的硬規則）的靜態防線。

規則本身寫得很清楚，但**沒有任何東西在檢查**，而它防的失敗形態偏偏是最難發現
的那種：半寫入的檔案不會讓讀取端崩潰，只會讓它**讀成別的內容**——半寫的佇列讀
成空的、於是批次「乾淨地」結束並回 rc=0；半寫的設定檔讓載入器整份退回預設值，
而使用者被告知編輯成功。實際踩過的漏網：`webrunner.pid`（dashboard 與
`verify_browser` 都讀）、批次標籤檔（dashboard 讀）、暫停標記的 webrunner 那一側
（bot 那側一直是原子的，兩邊不一致）。

這支測試用 AST 找出「對已知跨行程檔案常數的寫入」，要求它們走原子寫入家族。

**唯一的例外是 todo 佇列**，而且是刻意的：使用者常把佇列檔開在編輯器裡看它即時
消化，`os.replace` 會換掉 inode，編輯器會判定「檔案被刪掉又重建」而每次 pop 都
跳對話框。就地覆寫則讓沒有未存編輯的開啟檔靜默重載。理由與代價寫在
`_webrunner_shared.write_todo_characters` 的 docstring 與 `CLAUDE.md` 裡；這裡把
它列成明確的白名單，才不會有人「順手修好」它。
"""
import ast
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
# 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄）。
TEST_ROOT = Path(__file__).resolve().parent

# 這些常數指向**跨行程**檔案：一個行程寫、另一個行程輪詢或重讀。
_CROSS_PROCESS_CONSTANTS = {
    "WEBRUNNER_PID_FILE", "WEBRUNNER_PAUSE_FILE", "BATCH_LABEL_FILE",
    "SCHEDULE_FILE", "FAVORITES_FILE", "RECENT_IMAGE_MSGS_FILE",
    # 延後啟動（`/run in`／`/run at`）的落地檔：讀寫都是 bot，列在這裡是為了撐過
    # 重啟（同 `FAVORITES_FILE`），半寫入的檔在啟動時讀到就是一次靜默遺失的排程。
    "SCHEDULED_RUN_FILE",
    # 「斷網停下來、網路回來就接續」的落地檔：同上，讀寫都是 bot、為了撐過重啟。
    # 半寫入的檔在啟動時讀到，就是一個使用者沒叫停、卻再也不會接續的批次。
    "NETWORK_RESUME_FILE",
    # 排隊中／停放中的 Dorossi 提問。真的跨行程（`dashboard_server` 也讀它），而且
    # 2026-09-23 起它還扛著「撞到方案用量上限的單輪回合，時間到自己重跑」——半寫入的
    # 檔在啟動時讀到，就是一題 bot 明確答應過會自己跑、卻再也不會跑的提問。
    "DOROSSI_QUEUE_FILE",
    "PROGRESS_FILE", "BATCH_CONFIG_FILE", "SINGLE_IMAGE_REQUEST_FILE",
    "DOM_REQUEST_FILE",
    # 事件檔是整套系統裡最跨行程的一個：背景程式 `emit_event` 附加、bot 用
    # `_event_offset` 追位移讀。它原本**根本不在這份清單裡**，所以輪替時的
    # truncate-then-write 從來沒被查過。
    "EVENTS_FILE", "AUDIT_FILE",
    # 每日檢查讀回來的後端模型目錄：讀寫都是 bot，列在這裡是為了**撐過重啟**
    # （同 `FAVORITES_FILE`）。半寫入的檔在下次啟動讀到就是一份安靜退回內建表的
    # 目錄，而使用者只會發現「昨天選得到的模型今天不見了」，沒有任何錯誤訊息。
    "DOROSSI_MODEL_CATALOG_FILE",
}

# 允許的寫入方式（同目錄 temp → os.replace 的各家實作）。
_ATOMIC_WRITERS = {
    "_atomic_write_text", "_atomic_write_bytes", "_safe_write",
    "atomic_write_text", "_atomic_write", "_atomic_write_config",
    "os.replace", "replace",
}

# 刻意非原子的例外，連理由一起列管。
_DELIBERATE_EXCEPTIONS = {
    "write_todo_characters": (
        "todo 佇列刻意就地覆寫：os.replace 換掉 inode 會讓開著檔案的編輯器每次 "
        "pop 都跳「檔案被刪掉又重建」對話框。見該函式的 docstring 與 CLAUDE.md。"),
}

# 掃描範圍是**算出來的**，不是手寫的模組清單。
#
# 2026-09-10 之前這裡是一個寫死的六元組（`discord_bot` / `_webrunner_shared` /
# `_run_progress` / `_batch_config` ＋兩個 webrunner）。規則本身
# （`CLAUDE.md`「跨行程檔案一律原子寫入」）**沒有提到任何模組**，所以任何住在那
# 六個以外的寫入端都在守門的視野外——而且真的有：`webrunner.pid` 這個本檔開頭
# 就點名踩過的漏網，**唯一的寫入端是 repo root 的 `start_webrunner.py`**
# （第 143 行），六元組連 repo root 都不看。今天它剛好是原子的；把它改成
# `WEBRUNNER_PID_FILE.write_text(str(pid))`，整套測試不會有任何一支變紅。
#
# 同一個形狀在本專案已經記過六次。判準很簡單：
# **規則的文字有沒有提到模組？沒有的話，掃描就不能只看其中幾個。** 注意本檔下面
# 的 `test_text_io_always_names_its_encoding` 一直都是 `PKG_ROOT.glob("*.py")`
# ＋一支專門補 repo root 的測試——同一個檔案裡，兩支守門對「範圍」的答案不一樣，
# 這種內部不一致本身就是訊號。
def _project_sources() -> tuple[Path, ...]:
    """所有**產品端**模組：套件內 ＋ repo root 的啟動器／入口腳本。

    排除測試與 `conftest`：它們不是跨行程檔案的寫入端，而且會為了驗證掃描器而
    刻意寫出違規的合成原始碼。

    `test/` 照同一個判準過濾：兩支手動 e2e 腳本（`_test_*.py`）在 2026-09-22 之前住在
    套件裡、在這個範圍內，搬到 `test/` 之後照舊。
    """
    package = [p for p in (*sorted(PKG_ROOT.glob("*.py")), *sorted(TEST_ROOT.glob("*.py")))
               if not p.name.startswith("test_") and p.name != "conftest.py"]
    root = [p for p in sorted(PKG_ROOT.parent.glob("*.py"))
            if not p.name.startswith("test_")]
    return tuple(package + root)


def _all_project_modules() -> tuple[Path, ...]:
    """獨立於 `_project_sources()` 的完整列舉，給範圍釘樁用。

    刻意**不共用**上面那支的過濾條件：範圍釘樁要能在「有人把掃描範圍縮回去」
    的時候紅，所以它必須從別的地方數起。
    """
    return tuple(sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py"))
                 + sorted(PKG_ROOT.parent.glob("*.py")))


def _module_level_constants(path: Path) -> set[str]:
    """`path` 在**模組層級**定義了哪些全大寫名字。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                out.add(target.id)
    return out


def _scan_label(path: Path) -> str:
    # `test/` 裡的也用裸檔名：搬家前它們在套件裡，參數化的 id 照舊。
    return path.name if path.parent in (PKG_ROOT, TEST_ROOT) else f"../{path.name}"


_SOURCES = _project_sources()
_SOURCE_IDS = [_scan_label(p) for p in _SOURCES]
# 掃描集合掃成空的話，下面兩支參數化測試會**一個案例都不產生**，而 pytest 對
# 「零個案例」是綠的。這是最安靜的失效方式，所以下限要獨立釘。
_SCAN_FLOOR = 25


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    best = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.name
    return best


def _non_atomic_writes(path: Path) -> list[tuple[int, str, str]]:
    """`[(行號, 常數名, 所在函式), …]`——對跨行程檔案的非原子寫入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("write_text", "write_bytes"):
            continue
        target = func.value
        name = target.id if isinstance(target, ast.Name) else ""
        if name not in _CROSS_PROCESS_CONSTANTS:
            continue
        found.append((node.lineno, name, _enclosing_function(tree, node.lineno)))
    return found


# 這些常數指向**跨行程的目錄**。目錄底下的檔名是**算出來的**（`DIR / relpath`），
# 所以上面那份以「常數名稱」列舉的掃描對它們完全是盲的——名稱掃描只看得到「被寫
# 下來的名字」，看不到「被算出來的目標」。
#
# 2026-09-07 就是這樣漏掉一個真缺陷：`_sync_chrome_profile_back` 用
# `shutil.copy2(src, CHROME_PROFILE_DIR / relpath)` 覆蓋登入用的 `Cookies`。
# `copy2` 是「開目標為 wb 立刻截斷」，中途被砍（`/stop`、`taskkill /F`、當機）
# 留在磁碟上的是**半個 Cookies**——而下一輪 Chrome **不會報錯**，它會安靜地以
# 未登入狀態開機。正是這條硬規則要防的「不是崩潰，是安靜的錯誤結果」。
_CROSS_PROCESS_DIR_CONSTANTS = {
    # 登入態。webrunner 收工時寫回、下一輪啟動時（以及 verify_browser）讀。
    "CHROME_PROFILE_DIR",
}

# 會**截斷目標**的寫入方式。`shutil.copy2` 之所以危險正是因為它看起來像「複製」
# 而不像「寫入」——名字裡沒有 write，所以上面那些以 `write_text` / `write_bytes`
# 為判準的掃描全都掃不到它。
_TRUNCATING_COPIERS = {"copy", "copy2", "copyfile"}


def _paths_derived_from_dir_constants(scope: ast.AST) -> set[str]:
    """在**這一個函式裡**，哪些區域變數是直接從跨行程目錄常數算出來的。

    判準刻意是「直接」——`dst = CHROME_PROFILE_DIR / relpath` 算，
    `tmp = dst.with_name(dst.name + ".tmp")` **不算**。那個區別就是這支守門的
    全部精髓：同目錄 temp 是從 `dst` 推出來的、不是從常數推出來的，所以
    「寫進 temp 再 `os.replace`」自然不會被誤報，而「直接寫進最終目標」會。
    這樣就不需要「這個函式裡有沒有出現 os.replace」那種鬆散的放行條件——那種
    條件會放過「同一個函式裡別的地方剛好用了 os.replace」的情況。

    **一定要逐函式做，不能整個模組一起做。** 第一版掃整棵樹，於是
    `_sync_chrome_profile_back` 裡的 `dst = CHROME_PROFILE_DIR / relpath` 會讓
    `dst` 這個**名字**在整個模組變成「最終目標」，連帶把
    `_snapshot_chrome_profile` 裡完全無關的 `dst = dst_root / fname`（目標在
    snapshot 目錄，方向還是相反的）一起誤報。**變數名是 function-local 的，
    分析也必須是。** 這個誤報是這支守門第一次真的跑就抓到的——它同時證明了
    canary 為什麼要用真的原始碼跑一次，而不是只餵合成片段。
    """
    derived: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        if names & _CROSS_PROCESS_DIR_CONSTANTS:
            derived.add(target.id)
    return derived


def _derived_path_writes(path: Path) -> list[tuple[int, str, str]]:
    """`[(行號, 寫法, 函式名), …]`——直接覆寫跨行程目錄底下最終目標的寫入。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: list[tuple[int, str, str]] = []
    scopes = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        finals = _paths_derived_from_dir_constants(scope)
        if not finals:
            continue
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # shutil.copy2(src, <最終目標>)
            if (isinstance(func, ast.Attribute)
                    and func.attr in _TRUNCATING_COPIERS
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Name)
                    and node.args[1].id in finals):
                found.append((node.lineno, f"{func.attr}(…, {node.args[1].id})",
                              _enclosing_function(tree, node.lineno)))
            # <最終目標>.write_text(...) / .write_bytes(...)
            if (isinstance(func, ast.Attribute)
                    and func.attr in ("write_text", "write_bytes")
                    and isinstance(func.value, ast.Name)
                    and func.value.id in finals):
                found.append((node.lineno, f"{func.value.id}.{func.attr}(…)",
                              _enclosing_function(tree, node.lineno)))
            # open(<最終目標>, "w")
            if (isinstance(func, ast.Name) and func.id == "open"
                    and node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in finals):
                found.append((node.lineno, f"open({node.args[0].id}, …)",
                              _enclosing_function(tree, node.lineno)))
    return found


def _indirect_non_atomic_writes(path: Path) -> list[tuple[int, str, str]]:
    """`[(行號, 常數名, 函式名), …]`——**透過參數**對跨行程檔案的非原子寫入。

    直接掃常數名是不夠的。`discord_bot._rotate_ndjson_tail(path)` 寫的是
    `path.write_bytes(tail)`，而 `path` 是參數；呼叫端才是
    `_rotate_ndjson_tail(EVENTS_FILE)`。這條路上一個名字都對不起來，所以
    name-based 的掃描完全看不到它——事件檔的輪替因此當了很久的漏網之魚。

    作法：先找出「對自己某個參數做非原子寫入」的函式與參數位置，再看有沒有人
    用跨行程常數餵那個位置。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    # 函式名 -> {參數位置: 行號}
    writes_param: dict[str, dict[int, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in node.args.args]
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("write_text", "write_bytes"):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if func.value.id in params:
                writes_param.setdefault(node.name, {})[
                    params.index(func.value.id)] = inner.lineno
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        positions = writes_param.get(node.func.id)
        if not positions:
            continue
        for pos, lineno in positions.items():
            if pos < len(node.args):
                arg = node.args[pos]
                if (isinstance(arg, ast.Name)
                        and arg.id in _CROSS_PROCESS_CONSTANTS):
                    found.append((lineno, arg.id, node.func.id))
    return found


@pytest.mark.parametrize("path", _SOURCES, ids=_SOURCE_IDS)
def test_cross_process_files_are_written_atomically(path):
    source = _scan_label(path)
    if not path.is_file():
        pytest.skip(f"{source} 不存在")
    violations = [
        f"{source}:{lineno} 直接寫 `{const}`（在 `{func or '<module>'}` 裡）"
        for lineno, const, func in _non_atomic_writes(path)
        if func not in _DELIBERATE_EXCEPTIONS
    ] + [
        f"{source}:{lineno} 經由 `{func}()` 的參數寫 `{const}`"
        for lineno, const, func in _indirect_non_atomic_writes(path)
        if func not in _DELIBERATE_EXCEPTIONS
    ]
    assert not violations, (
        "以下跨行程檔案不是原子寫入：\n  " + "\n  ".join(violations)
        + "\n改走 `_atomic_write_text` / `_run_progress.atomic_write_text` "
          "（同目錄 temp → `os.replace`）。半寫入不會讓讀取端崩潰，只會讓它讀成"
          "別的內容——那是沒有人看得出來的錯誤。"
          "\n真的要例外就寫進 `_DELIBERATE_EXCEPTIONS`，連理由一起。")


@pytest.mark.parametrize("path", _SOURCES, ids=_SOURCE_IDS)
def test_files_under_cross_process_directories_are_written_atomically(path):
    """跨行程**目錄**底下的最終目標，同樣不得被直接覆寫。

    上面那兩支掃的是「具名常數」與「透過參數傳進去的常數」。兩支都看不到
    `CHROME_PROFILE_DIR / relpath` 這種**算出來**的目標——名稱掃描只看得到被寫
    下來的名字。2026-09-07 一個真缺陷就是這樣躺著沒被抓到：
    `_sync_chrome_profile_back` 直接 `shutil.copy2` 覆蓋登入用的 `Cookies`，
    中途被砍就留下半個檔案，而下一輪 Chrome 不報錯、安靜地以未登入狀態開機。

    順帶補上 `shutil.copy2` 這個寫法本身——它看起來像「複製」不像「寫入」，
    名字裡沒有 write，所以以 `write_text` 為判準的掃描一律漏掉它，但它確確實實
    會把目標開成 wb 並立刻截斷。
    """
    source = _scan_label(path)
    offenders = _derived_path_writes(path)
    assert not offenders, "\n".join(
        f"{source}:{lineno} {how}（在 {func} 裡）" for lineno, how, func in offenders
    ) + (
        "\n\n這是寫進跨行程目錄底下的**最終目標**。要走同目錄 temp → "
        "`os.replace`：先寫進 `dst.with_name(dst.name + '.tmp')`，成功再 replace，"
        "失敗要把 temp 收掉。範本見 `_run_progress._atomic_write`。")


def test_the_derived_path_scanner_actually_catches_the_shape_it_was_written_for():
    """掃描器自己的 canary。

    「寫完就通過」不是好消息，是還沒被驗過——尤其這一支在修好之後的 repo 上永遠
    是綠的，所以完全不能證明它有在做事。這裡餵合成原始碼：**直接寫最終目標**要
    抓到，**寫同目錄 temp 再 replace** 不得誤報。

    第二個案例正是這支守門的精髓：temp 是從 `dst` 推出來的、不是從常數推出來的，
    所以「直接從常數算出來的變數」這個判準天然把它排除，不需要「函式裡有沒有出現
    `os.replace`」那種會放過別處誤用的鬆散條件。
    """
    bad = """
import shutil
def sync(snapshot):
    for relpath in FILES:
        dst = CHROME_PROFILE_DIR / relpath
        shutil.copy2(snapshot / relpath, dst)
"""
    good = """
import os, shutil
def sync(snapshot):
    for relpath in FILES:
        dst = CHROME_PROFILE_DIR / relpath
        tmp = dst.with_name(dst.name + ".sync.tmp")
        shutil.copy2(snapshot / relpath, tmp)
        os.replace(tmp, dst)
"""
    import tempfile
    for label, src, expect_hit in (("直接覆寫", bad, True),
                                   ("temp→replace", good, False)):
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = Path(tmpdir) / "probe.py"
            probe.write_text(src, encoding="utf-8")
            hits = _derived_path_writes(probe)
        assert bool(hits) is expect_hit, (
            f"「{label}」這個形狀判斷錯了（掃到 {hits}）")


def test_the_directory_constant_list_is_not_empty():
    """清單掃成空的話上面那支永遠會過——最安靜的失效方式。"""
    assert _CROSS_PROCESS_DIR_CONSTANTS, (
        "`_CROSS_PROCESS_DIR_CONSTANTS` 空了，那支守門等於沒有。")
    assert _TRUNCATING_COPIERS, "`_TRUNCATING_COPIERS` 空了。"


_ALL_CROSS_PROCESS_NAMES = _CROSS_PROCESS_CONSTANTS | _CROSS_PROCESS_DIR_CONSTANTS

# 2026-09-10 之前的掃描範圍，只留給下面的控制組當「縮回去會怎樣」的實例。
_LEGACY_SOURCES = tuple(PKG_ROOT / name for name in (
    "discord_bot.py", "_webrunner_shared.py", "_run_progress.py",
    "_batch_config.py", "webrunner_novelai.py", "webrunner_je_only.py"))


def _blind_modules(scanned) -> dict[str, list[str]]:
    """有指名跨行程檔案、卻**不在掃描集合裡**的模組。

    列舉刻意走 `_all_project_modules()`——跟 `_project_sources()` 是兩條路。範圍
    釘樁如果跟被釘的東西共用同一個列舉器，就只會證明「它等於它自己」。
    """
    inside = {p.resolve() for p in scanned}
    blind: dict[str, list[str]] = {}
    for path in _all_project_modules():
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        named = _module_level_constants(path) & _ALL_CROSS_PROCESS_NAMES
        if named and path.resolve() not in inside:
            blind[_scan_label(path)] = sorted(named)
    return blind


def _unbacked_constants(scanned, wanted) -> list[str]:
    """`wanted` 裡有哪些名字，掃描集合中沒有任何模組在模組層級定義。"""
    defined: set[str] = set()
    for path in scanned:
        defined |= _module_level_constants(path)
    return sorted(name for name in wanted if name not in defined)


def _assert_scan_floor(scanned) -> None:
    assert len(scanned) >= _SCAN_FLOOR, (
        f"掃描集合只剩 {len(scanned)} 個模組（下限 {_SCAN_FLOOR}）——列舉器壞了，"
        "或是範圍又被縮回手寫清單。參數化測試在**零個案例**時是綠的，所以這個"
        "下限是它唯一的訊號。")


def test_the_scanned_population_is_big_enough_to_be_the_whole_project():
    _assert_scan_floor(_SOURCES)


def test_the_floor_fires_when_the_enumerator_comes_back_empty():
    """控制組：把列舉器換成空的，下限必須紅——而且要紅在這一句上。"""
    with pytest.raises(AssertionError, match="掃描集合只剩"):
        _assert_scan_floor(())


def test_the_scan_reaches_every_module_that_names_a_cross_process_file():
    """範圍釘樁：規則沒點名模組，掃描就不能只看其中幾個。

    這一支要防的不是今天的違規（今天一個都沒有），而是**看不見**本身。真實案例：
    `webrunner.pid` 的唯一寫入端是 repo root 的 `start_webrunner.py`，而
    2026-09-10 之前的掃描範圍是一個寫死的六元組，連 repo root 都不在裡面。
    今天那一行剛好是原子的，所以「範圍窄」和「範圍寬」在乾淨資料上長得一模一樣
    ——那正是範圍**自己**需要一根樁的原因。
    """
    blind = _blind_modules(_SOURCES)
    assert not blind, (
        "以下模組指名了跨行程檔案，卻在掃描範圍外：\n  "
        + "\n  ".join(f"{name}: {', '.join(names)}"
                      for name, names in sorted(blind.items()))
        + "\n把 `_project_sources()` 改回涵蓋它們，不要用手寫清單。")


def test_the_scope_pin_fires_when_the_scan_narrows_back_to_a_hardcoded_list():
    """控制組：餵它 2026-09-10 之前的六元組，釘樁必須指出漏掉了誰。"""
    blind = _blind_modules(_LEGACY_SOURCES)
    assert "../start_webrunner.py" in blind, (
        "拿舊的六元組來掃，`start_webrunner.py`（`webrunner.pid` 的唯一寫入端）"
        f"必須被指認成盲區，實際掃到的是 {sorted(blind)}")
    assert "WEBRUNNER_PID_FILE" in blind["../start_webrunner.py"]


def test_no_cross_process_constant_name_has_gone_stale():
    """對帳：清單裡的每個名字都要還指得到東西。

    這份清單是**失效時會靜默放行**的那種：把 `EVENTS_FILE` 改個名字，這裡的字串
    就從此對不到任何節點——守門照跑、集合還在、每一支測試照樣綠，但那個檔案再也
    沒有被保護。跟 `CLAUDE.md` 裡 `_OWNER_ONLY_SLASH` 記載的失效形態完全一樣。
    """
    stale = _unbacked_constants(_SOURCES, _ALL_CROSS_PROCESS_NAMES)
    assert not stale, (
        f"沒有任何產品端模組定義這些名字：{stale}。"
        "常數被改名或刪掉了，清單裡的字串已經對不到任何東西——"
        "改成新名字，或連同保護一起移除。")


def test_the_reconciliation_fires_when_an_entry_names_nothing():
    """控制組：塞一個不存在的名字進去，對帳必須點名它。"""
    stale = _unbacked_constants(
        _SOURCES, {"WEBRUNNER_PID_FILE", "NO_SUCH_CROSS_PROCESS_FILE"})
    assert stale == ["NO_SUCH_CROSS_PROCESS_FILE"], (
        f"對帳沒抓到虛構的名字（回傳 {stale}）——它對真的改名一樣會沉默。")


def test_the_named_constant_scanner_catches_what_it_claims_to(tmp_path):
    """具名常數掃描器的 canary——它自己一直沒有控制組。

    修好之後的 repo 上這支掃描器永遠回空集合，所以「綠」完全不能證明它在做事：
    把 `_non_atomic_writes` 改成 `return []`，上面那 30 幾個參數化案例一個都不會
    紅。這裡餵合成原始碼，兩個方向都釘：**直接寫**要抓到、**temp → replace**
    不得誤報，而且「經由參數寫」那條間接路徑也要一起驗。
    """
    direct = tmp_path / "direct.py"
    direct.write_text(
        "def save(pid):\n"
        "    WEBRUNNER_PID_FILE.write_text(str(pid), encoding='utf-8')\n",
        encoding="utf-8")
    assert [c for _, c, _ in _non_atomic_writes(direct)] == [
        "WEBRUNNER_PID_FILE"], "直接寫跨行程常數沒被抓到"

    atomic = tmp_path / "atomic.py"
    atomic.write_text(
        "import os\n"
        "def save(pid):\n"
        "    tmp = WEBRUNNER_PID_FILE.with_suffix('.tmp')\n"
        "    tmp.write_text(str(pid), encoding='utf-8')\n"
        "    os.replace(tmp, WEBRUNNER_PID_FILE)\n",
        encoding="utf-8")
    assert not _non_atomic_writes(atomic), "同目錄 temp → replace 被誤報了"

    via_param = tmp_path / "via_param.py"
    via_param.write_text(
        "def _write(path, text):\n"
        "    path.write_text(text, encoding='utf-8')\n"
        "def save(pid):\n"
        "    _write(WEBRUNNER_PID_FILE, str(pid))\n",
        encoding="utf-8")
    assert [c for _, c, _ in _indirect_non_atomic_writes(via_param)] == [
        "WEBRUNNER_PID_FILE"], "經由參數寫跨行程常數沒被抓到"


def test_the_real_pid_file_writer_would_be_caught_if_it_stopped_being_atomic(
        tmp_path):
    """把**真的那個檔案**改壞一行，掃描必須紅。

    合成原始碼證明的是偵測邏輯；這一支證明的是「偵測邏輯真的對得上磁碟上那一行」
    ——中間任何一環（範圍、常數名、寫法）錯掉都會讓它沉默。`webrunner.pid` 是本檔
    開頭就點名踩過的漏網，而它的唯一寫入端住在 repo root。

    改的是**沙盒裡的副本**，不是磁碟上那一份：`start_webrunner.py` 正被長命的
    監督者行程執行，就地改它等於在生產環境上做實驗。
    """
    real = PKG_ROOT.parent / "start_webrunner.py"
    source = real.read_text(encoding="utf-8")
    anchor = "atomic_write_text(WEBRUNNER_PID_FILE"
    assert anchor in source, (
        f"`{anchor}…` 不在 `start_webrunner.py` 裡了。寫法換了就把這裡的錨點一起"
        "換掉——別把這支測試刪掉，它是這個檔案唯一的端對端證明。")
    broken = tmp_path / "start_webrunner.py"
    broken.write_text(
        source.replace(anchor + ", str(pid))",
                       "WEBRUNNER_PID_FILE.write_text(str(pid), encoding=\"utf-8\")"),
        encoding="utf-8")
    assert [c for _, c, _ in _non_atomic_writes(broken)] == ["WEBRUNNER_PID_FILE"], (
        "把 pid 檔的寫法改成 truncate-then-write，掃描竟然沒紅——"
        "掃描範圍或常數名對不上磁碟上那一行了。")


def test_the_todo_queue_exception_is_still_the_only_one_and_still_explained():
    """例外清單要小，而且每一筆都要有理由——這是它不會慢慢長大的唯一保障。"""
    assert set(_DELIBERATE_EXCEPTIONS) == {"write_todo_characters"}
    for name, reason in _DELIBERATE_EXCEPTIONS.items():
        assert len(reason) > 40, f"{name} 的例外理由太短，寫清楚為什麼"


def test_the_atomic_helpers_all_use_replace_onto_the_target():
    """每個實作都必須是「同目錄 temp → os.replace」，不能只是換個名字的覆寫。"""
    import _run_progress
    import _batch_config
    import discord_bot

    for module, name in ((_run_progress, "atomic_write_text"),
                         (_batch_config, "_atomic_write_config"),
                         (discord_bot, "_atomic_write_text"),
                         (discord_bot, "_atomic_write_bytes")):
        source = ast.parse(
            Path(module.__file__).read_text(encoding="utf-8"))
        target = next(
            (node for node in ast.walk(source)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == name), None)
        assert target is not None, f"{module.__name__} 少了 {name}"
        body = ast.unparse(target)
        assert "os.replace" in body, (
            f"{module.__name__}.{name} 沒有用 `os.replace`，那就不是原子寫入")
        # 有的實作把 temp 路徑放在模組常數裡（`_BATCH_CONFIG_TMP`），所以不分
        # 大小寫比對。
        assert "tmp" in body.lower(), (
            f"{module.__name__}.{name} 應該先寫同目錄的 temp 檔")


def test_atomic_write_actually_replaces_and_leaves_no_temp(tmp_path):
    """行為驗證：寫完之後目標是新內容，且 .tmp 不留在磁碟上。"""
    import _run_progress

    target = tmp_path / "state.json"
    assert _run_progress.atomic_write_text(target, '{"a": 1}') is True
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert _run_progress.atomic_write_text(target, '{"a": 2}') is True
    assert target.read_text(encoding="utf-8") == '{"a": 2}'
    assert not (tmp_path / "state.json.tmp").exists()


def test_ndjson_rotation_is_atomic_and_keeps_whole_lines(tmp_path):
    """輪替是整檔改寫，中途死掉就是整段事件歷史歸零——所以它也要走原子寫入。

    順帶釘住兩件本來就該成立的事：留下來的每一行都是完整的 JSON（seek 進去的
    那半行要丟掉），以及 `.tmp` 不留在磁碟上。
    """
    import json
    import discord_bot

    path = tmp_path / "events.ndjson"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(400)),
                    encoding="utf-8")
    size = path.stat().st_size
    new_size = discord_bot._rotate_ndjson_tail(path, keep_bytes=size // 4)
    assert new_size is not None, "超過 2 倍門檻就該輪替"
    text = path.read_text(encoding="utf-8")
    assert text.endswith('{"n": 399}\n'), "最新的事件必須留著"
    for line in text.splitlines():
        json.loads(line)          # 半截行會在這裡炸掉
    assert not (tmp_path / "events.ndjson.tmp").exists(), "temp 檔要收乾淨"


def test_ndjson_rotation_leaves_a_small_file_alone(tmp_path):
    """沒超過門檻就不要動它——每一次改寫都是一個可能弄丟事件的窗口。"""
    import discord_bot

    path = tmp_path / "events.ndjson"
    path.write_text('{"n": 1}\n', encoding="utf-8")
    assert discord_bot._rotate_ndjson_tail(path, keep_bytes=4096) is None
    assert path.read_text(encoding="utf-8") == '{"n": 1}\n'


def test_atomic_write_reports_failure_instead_of_raising(tmp_path):
    """寫不進去要回 False，不能丟例外——呼叫端多半在 finally / 背景迴圈裡。"""
    import _run_progress

    missing_dir = tmp_path / "no-such-dir" / "state.json"
    assert _run_progress.atomic_write_text(missing_dir, "x") is False


# --------------------------------------------------------------------------
# 文字 I/O 一律指定 encoding（同一類「安靜的錯誤結果」）
# --------------------------------------------------------------------------
# 沒寫 `encoding=` 的文字 I/O 走的是**行程 locale 的預設編碼**，在這台開發機上
# 是 `cp950`（`locale.getpreferredencoding(False)`，Python 3.14 仍未預設 UTF-8）。
# 而本專案的檔案內容幾乎全是 UTF-8 中文：佇列、提示詞、設定檔、log。後果分兩種，
# 都很難追：讀到不能用 cp950 表示的字元會 `UnicodeDecodeError`（例如日文假名、
# emoji、`\xa0`），能表示的則**安靜地變成亂碼**存回去。
# 更糟的是它跟機器綁定——開發機設成 UTF-8 beta 模式（`PYTHONUTF8=1`）就完全復現
# 不出來，只有別人的機器會壞。
_TEXT_IO_ATTRS = {"open", "read_text", "write_text"}

# 這些名稱底下的 `open` / `read_text` 不是檔案系統文字 I/O，逐筆列管。
_NOT_FILESYSTEM_RECEIVERS = {
    "os": "`os.open` 回的是 fd 不是文字串流，編碼由之後的 `os.write` 決定。",
    "Image": "PIL 的 `Image.open` 讀的是影像位元組。",
    "fake_grab": "測試裡代替 PIL 的假物件，同上。",
    "gui": "`gui.read_text()` 是螢幕文字辨識，不碰檔案。",
    # `importlib.metadata.Distribution.read_text(filename)` 讀的是**發行版中繼
    # 資料目錄裡**的檔案，接收者是 `Distribution` 不是 `Path`。實查本機 CPython
    # 3.14.4 的 `PathDistribution.read_text`：內部就是
    # `...read_text(encoding='utf-8')` 寫死的，而且它的簽名**沒有** `encoding`
    # 參數——傳進去是 `TypeError`。也就是說這一筆不是「忘了指定」，是「指定不
    # 了，而且已經是對的那個編碼」。
    "dist": "`importlib.metadata.Distribution.read_text` 內部寫死 utf-8，"
            "簽名不收 `encoding=`。",
}


def _text_io_offenders(tree: ast.Module) -> list[tuple[int, str]]:
    """回傳 (行號, 呼叫描述)——文字模式、卻沒指定 `encoding=` 的 I/O。"""
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            receiver, attr, mode_index = "", "open", 1
        elif isinstance(func, ast.Attribute) and func.attr in _TEXT_IO_ATTRS:
            receiver = ast.unparse(func.value)
            if receiver in _NOT_FILESYSTEM_RECEIVERS:
                continue
            attr, mode_index = func.attr, 0
        else:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if "encoding" in keywords:
            continue
        mode = keywords.get("mode")
        if mode is None and attr == "open" and len(node.args) > mode_index:
            mode = node.args[mode_index]
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            continue          # 二進位模式沒有編碼可言
        label = f"{receiver}.{attr}" if receiver else attr
        bad.append((node.lineno, label))
    return bad


# 套件 ＋ `test/`：測試檔在 2026-09-22 之前住在套件裡，一直都在這一支的範圍內，
# 搬家之後範圍照舊（參數化的 id 也照舊是裸檔名）。
_TEXT_IO_SOURCES = {p.name: p for p in (*PKG_ROOT.glob("*.py"),
                                        *TEST_ROOT.glob("*.py"))}


@pytest.mark.parametrize("source", sorted(_TEXT_IO_SOURCES))
def test_text_io_always_names_its_encoding(source):
    """本專案的檔案是 UTF-8 中文，不能讓 locale 決定怎麼解碼。

    這條跟原子寫入是同一類問題：懲罰不是當場崩潰，而是**安靜的錯誤結果**
    ——半個佇列讀成空的、中文設定值寫回去變成亂碼。
    """
    tree = ast.parse(_TEXT_IO_SOURCES[source].read_text(encoding="utf-8"))
    offenders = _text_io_offenders(tree)
    assert not offenders, (
        f"{source} 的這些文字 I/O 沒有指定 `encoding=`，會用 locale 預設值"
        f"（本機是 cp950）："
        + "、".join(f"第 {line} 行 `{label}`" for line, label in offenders)
        + "。補上 `encoding=\"utf-8\"`；真的不是檔案系統 I/O 就把接收者列進 "
        "`_NOT_FILESYSTEM_RECEIVERS` 並寫清楚理由。")


def test_the_launchers_are_covered_too():
    """repo root 的腳本不在 `axiomatic/` 的 glob 裡，會被上面漏掉。

    **名字寫「啟動器」，範圍是整個 repo root。** 2026-09-20 以前這裡真的只掃
    `start_*.py`，於是 `install_autostart.py`／`run_batch.py` 兩支正式腳本的
    文字 I/O 從來沒有被這一支看過（同檔的 `test_text_io_always_names_its_encoding`
    只掃套件目錄）。編碼規則是跨領域的，範圍就該是整個 root。名字保留是因為
    別處的說明引用了它——改名會讓那些說明指向一個
    不存在的符號。
    """
    for path in sorted(PKG_ROOT.parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not _text_io_offenders(tree), path.name


_TEXT_IO_BAD = (
    ("裸 open", "open('x')"),
    ("open 帶文字模式", "open('x', 'r')"),
    ("open 帶 mode 關鍵字", "open('x', mode='w')"),
    ("Path.read_text", "Path('x').read_text()"),
    ("Path.write_text", "Path('x').write_text('y')"),
    ("p.open() 文字模式", "p.open('r')"),
)


@pytest.mark.parametrize("label,expr", _TEXT_IO_BAD,
                         ids=[c[0] for c in _TEXT_IO_BAD])
def test_the_text_io_detector_still_fires(label, expr):
    """每一種「沒指定編碼的文字 I/O」都要抓得到。

    ⚠️ **這一組是實測補上的。** `_text_io_offenders` 的兩個呼叫端餵的都是**真實
    專案樹**，而專案是乾淨的——所以把整個函式換成 `return []`，這個檔案裡每一支
    測試都還是綠的。一條跨領域硬規則，卻沒有任何東西證明它的偵測器還活著。
    2026-09-10 在 `test_verify_browser` 上實際出現過三次同型的缺口。
    """
    assert _text_io_offenders(ast.parse(expr)), f"{label}：沒抓到"


_TEXT_IO_OK = (
    ("指定了編碼", "open('x', encoding='utf-8')"),
    ("read_text 指定編碼", "Path('x').read_text(encoding='utf-8')"),
    ("二進位模式沒有編碼可言", "open('x', 'rb')"),
    ("write_bytes 不是文字 I/O", "Path('x').write_bytes(b'y')"),
    ("p.open() 二進位", "p.open('rb')"),
    ("os.open 回的是 fd", "os.open('x', 0)"),
    ("名單內的接收者：螢幕文字辨識", "gui.read_text()"),
    ("名單內的接收者：影像位元組", "Image.open('x.png')"),
)


@pytest.mark.parametrize("label,expr", _TEXT_IO_OK,
                         ids=[c[0] for c in _TEXT_IO_OK])
def test_the_text_io_detector_does_not_cry_wolf(label, expr):
    """反面：合法的寫法一個都不准被判成違規。

    這一組釘的是三個豁免（`encoding=`、二進位模式、非檔案系統接收者名單）。
    少了它們，把任何一個豁免拿掉都不會有症狀——真實資料上這些寫法本來就已經
    是合規的，寬與窄的判準給出同一個答案。本檔開頭記著更寬的版本曾經六筆命中
    有四筆是誤判，這一組就是不讓它退回去的那道欄杆。
    """
    assert not _text_io_offenders(ast.parse(expr)), f"{label}：誤判了"


def test_the_non_filesystem_allowlist_stays_justified():
    """名單是列管制，每一筆都要有理由，不是「加進來就不會紅」的垃圾桶。"""
    for receiver, reason in _NOT_FILESYSTEM_RECEIVERS.items():
        assert reason.strip(), receiver
    assert len(_NOT_FILESYSTEM_RECEIVERS) <= 8, (
        "名單長到這個地步，通常代表偵測寫錯了而不是真的有那麼多例外。")


# ---------------------------------------------------------------------------
# `CLAUDE.md` 的涵蓋清單 ↔ `_CROSS_PROCESS_CONSTANTS`
#
# 上面那份常數是**守門實際在用**的集合；`CLAUDE.md` 的「Atomic writes for
# cross-process files」那一段是**規則的正本**——冷啟動的 session 與 subagent 在判斷
# 「我新加的這個檔案要不要原子寫入」時讀的是它，不是這裡。
#
# 2026-09-11 之前兩邊從來沒有比對過，而它們**已經不一致了**：正本用散文列了八項，
# 守門蓋了十二個常數。差的四個是 `SINGLE_IMAGE_REQUEST_FILE`、`AUDIT_FILE`、
# `FAVORITES_FILE`、`RECENT_IMAGE_MSGS_FILE`。
#
# ⚠️ 差最要緊的是第一個，而且它不是邊角：`single_image_request.json` 是 **bot 寫、
# webrunner 讀並刪**的核心磁碟契約（`discord_bot` 的 `os.replace` ↔
# `_webrunner_shared.serve_single_image_request` 讀完 `unlink`），也就是這條規則最
# 典型的對象，卻正好是規則自己沒列到的那一個。讀那一段的人看到的是「佇列／pid／
# 設定檔」，很容易推論出「一個**請求**檔不算」。
#
# 這是 `_OWNER_ONLY_SLASH` 與 `_pid_alive` 那個形狀的第三次：**正本錯了沒有任何
# 症狀**——守門照跑、測試照綠，因為守門讀的是常數不是文件。而這一對清單在本 repo
# 已經賠過一次：`.tmp` sibling 那支守門原本自己手打第二份，落後三個常數，留下四個
# 沒被 ignore 的 `.tmp`。那次的修法是讓下游**推導**
# 自常數；散文推導不了，所以只能對帳。
#
# 反方向也要守，而且理由不同：常數集合刻意比「跨行程」更寬（`FAVORITES_FILE` /
# `RECENT_IMAGE_MSGS_FILE` 只有 bot 一個讀寫者，收在裡面是為了重啟後的耐久性），
# 而那個理由**只寫在 CLAUDE.md**。少了反方向，有人「整理」掉那兩個常數不會有症狀。
#
# 常數名本身的過期不必在這裡再查一次——`test_no_cross_process_constant_name_has_gone
# _stale` 已經在比常數 ↔ 磁碟，而這一支讓文件 == 常數，所以那道檢查會遞移過去。
# ---------------------------------------------------------------------------
_DOC_COVERED_RE = re.compile(
    r"\*\*The covered set, by constant name\*\*(.+?)\n\n", re.DOTALL)


def _covered_names_in(text: str) -> set[str]:
    """一段文字裡、那個標題底下、反引號包起來的 ALL_CAPS 常數名。

    只收全大寫是為了濾掉同一段裡的
    `` `test_atomic_writes._CROSS_PROCESS_CONSTANTS` `` 與
    `` `_rotate_ndjson_tail` ``——它們不是被涵蓋的檔案，是指路。濾不掉的話反方向
    會出現兩筆假的「文件有、常數沒有」，而最省事的修法是放寬比對，那會讓整組
    對帳失去意義。

    ⚠️ **吃字串而不是自己讀檔，是為了讓下面那個下限驗得到。** 原本這支自己
    `read_text` 磁碟上的 CLAUDE.md，於是合成語料餵不進去，而下限在真實資料上
    **恆真**——實測把 `>= 10` 放寬成 `>= 0` 是 SURVIVED。分開之後
    `test_the_covered_set_extractor_returns_nothing_when_the_shape_breaks`
    才有辦法餵壞掉的形狀。
    """
    match = _DOC_COVERED_RE.search(text)
    if not match:
        return set()
    return set(re.findall(r"`([A-Z][A-Z0-9_]*)`", match.group(1)))


def _documented_cross_process_constants() -> set[str]:
    return _covered_names_in(
        (PKG_ROOT.parent / "CLAUDE.md").read_text(encoding="utf-8"))


def _covered_set_drift(documented, guarded) -> tuple[list[str], list[str]]:
    """(文件有、守門沒有), (守門有、文件沒有)。

    ⚠️ 比較刻意搬進純函式。修好之後兩份清單一致，所以真實資料上這支回的是
    `([], [])`，呼叫端那兩句斷言**在乾淨資料上恆真**——把它們刪掉不會有任何測試
    變紅。下面 `test_the_covered_set_drift_comparison_actually_bites` 用合成語料釘
    住這支函式自己的邏輯。（本 repo 記過這個形狀：一份乾淨的清單會讓它自己的斷言
    變得測不到。）
    """
    unguarded = sorted(name for name in documented if name not in set(guarded))
    undocumented = sorted(name for name in guarded if name not in set(documented))
    return unguarded, undocumented


def test_the_claude_md_covered_set_is_extractable():
    """正對照：抽不到的話，下面那支退化成「空集合 vs 常數」，只會報一堆假的。

    抽取失敗的樣子跟「文件漏列了全部十二個」一模一樣，所以要先確認抽到的東西
    像話，再去談差異。下限取 10 而不是 12：真的加減一個跨行程檔案時不該卡在這裡，
    但形狀壞掉（標題改字、段落被拆開）會讓它掉到 0。
    """
    documented = _documented_cross_process_constants()
    assert len(documented) >= 10, (
        f"從 CLAUDE.md 抽到 {sorted(documented)}——"
        "「**The covered set, by constant name**」那一段的形狀變了"
        "（標題字面、或段落被空行拆開），下面的對帳等於沒在比。")


def test_the_covered_set_extractor_returns_nothing_when_the_shape_breaks():
    """合成對照：形狀壞掉時抽取要**歸零**，而不是靜靜抽到一半。

    ⚠️ 這一支才讓上面那個 `>= 10` 有意義。沒有它的時候，把下限放寬成 `>= 0`
    在乾淨資料上完全沒有症狀（**實測 SURVIVED**）——真的 CLAUDE.md 抽得到 12 個，
    下限寫多少都一樣。下限守的是「有人改了標題字面、或把那一段拆成兩段」，所以
    驗它的唯一辦法是餵壞掉的形狀進去。

    四格的值都是實際跑出來的，不是推的（尤其第三格：非貪婪的 `(.+?)\n\n` 會停在
    **第一個**空行，所以拆段之後掉的是後半那個名字，不是整段）。
    """
    good = ("**The covered set, by constant name** — `A_FILE`, `B_FILE`.\n"
            "More prose with `C_FILE`.\n\n後面另一段 `D_FILE`。\n")
    assert _covered_names_in(good) == {"A_FILE", "B_FILE", "C_FILE"}, (
        "同一段裡的名字要全收，下一段的不算。")
    assert _covered_names_in(good.replace("covered set", "covered files")) == set(), (
        "標題字面被改掉時必須抽不到——抽到別的段落比抽不到更糟。")
    split = good.replace(", `B_FILE`.\n", ", `B_FILE`.\n\n")
    assert _covered_names_in(split) == {"A_FILE", "B_FILE"}, (
        "那一段被空行拆開時，後半的名字會掉——這正是下限要抓的形狀。")
    pointer = ("**The covered set, by constant name** — `A_FILE`; see "
               "`test_atomic_writes._CROSS_PROCESS_CONSTANTS` and "
               "`_rotate_ndjson_tail` and `os.replace`.\n\n")
    assert _covered_names_in(pointer) == {"A_FILE"}, (
        "指路用的符號名不是涵蓋清單的成員。")


def test_the_claude_md_covered_set_matches_the_guard():
    """CLAUDE.md 的涵蓋清單 ↔ `_CROSS_PROCESS_CONSTANTS`，兩個方向。

    這一支存在的理由是它**當初就會抓到**：修之前正本列八項、守門蓋十二個，漏的
    四個裡包含 `SINGLE_IMAGE_REQUEST_FILE`——bot 寫、webrunner 讀並刪的核心磁碟
    契約，也就是這條規則最典型的對象。而整套測試那時是全綠的，因為沒有任何東西
    在比這兩份清單。
    """
    documented = _documented_cross_process_constants()
    assert documented, "抽不到文件那一側——見上一支。"
    assert _CROSS_PROCESS_CONSTANTS, "`_CROSS_PROCESS_CONSTANTS` 是空的。"
    unguarded, undocumented = _covered_set_drift(
        documented, _CROSS_PROCESS_CONSTANTS)
    assert not unguarded, (
        f"CLAUDE.md 列了這些常數，但 `_CROSS_PROCESS_CONSTANTS` 沒有："
        f"{unguarded}。要嘛把它加進守門，要嘛它已經改名／被刪掉了。")
    assert not undocumented, (
        f"`_CROSS_PROCESS_CONSTANTS` 蓋了這些，但 CLAUDE.md 沒列："
        f"{undocumented}。CLAUDE.md 是規則的正本——冷啟動的人是照它判斷"
        "「我這個新檔案要不要原子寫入」的，漏列不會讓任何測試變紅。")


def test_the_covered_set_drift_comparison_actually_bites():
    """合成對照：兩個方向各自要真的咬得到。

    沒有這一支，上面那兩句斷言是**不可證偽**的：清單一致時 `_covered_set_drift`
    永遠回 `([], [])`，把任一句（甚至兩句）刪掉都不會有測試變紅。這裡釘的是函式
    自己的邏輯，不是呼叫點的斷言——那個界線值得知道，但不必為了追它去加 AST 層的
    「這句斷言必須存在」檢查。
    """
    both = {"A_FILE", "B_FILE"}
    assert _covered_set_drift(both, both) == ([], [])
    # 文件有、守門沒有（常數改名或被刪，文件沒跟上）
    assert _covered_set_drift(both | {"GONE_FILE"}, both) == (["GONE_FILE"], [])
    # 守門有、文件沒有（這就是 2026-09-11 修掉的那個真實狀態）
    assert _covered_set_drift(both, both | {"NEW_FILE"}) == ([], ["NEW_FILE"])
    # 兩個方向同時壞掉時，兩邊都要報，不能只報先算的那一個
    assert _covered_set_drift({"X_FILE"}, {"Y_FILE"}) == (["X_FILE"], ["Y_FILE"])


# ---------------------------------------------------------------------------
# One name, one file: every process spells its cross-process path constants itself
# ---------------------------------------------------------------------------
#
# The bot, the batch, the dashboard and the launchers each write their own line
# `WEBRUNNER_PAUSE_FILE = PROJECT_ROOT / "webrunner.pause"` -- they may not import each other
# (the module boundary), so these constants can only be written once per process. Measured
# 2026-09-25: 25 names are defined in two or more modules, all naming the same file, and
# **nothing was comparing them**. Change one copy and both sides keep running, they just never
# meet again: the pause marker, the single-image request and the DOM request the bot writes are
# never read by the batch, with no error anywhere.
#
# What is compared is the **resolved path**, not the spelling: a launcher writes
# `REPO_ROOT / "webrunner.pid"`, the package writes `PROJECT_ROOT / "webrunner.pid"`, each root
# computed from its own `__file__`, and both are the same file.
#
# The blind spot: two processes giving one file **different** names. This compares same-named
# constants, so it cannot know about that case.

# Same name, deliberately a different file (or deliberately an opaque shape). Each entry needs
# its reason; once it resolves to the same file everywhere, it is reported as stale.
_SAME_NAME_DIFFERENT_FILE = {
    "LOCK_FILE": "Each supervisor's own single-instance lock (`start_webrunner.py` / "
                 "`start_discord_bot.py`); the bot's is per platform, so it is also an opaque shape.",
}
# 25 shared names measured on 2026-09-25.
_PATH_PARITY_FLOOR = 20


def _evaluate_path(node: ast.AST, env: dict, module_file: Path):
    """Evaluate a module-level path expression to an absolute path, or None (never a guess).

    Only these shapes are understood: `Path(__file__)`, `.resolve()`, `.parent`,
    `X / "literal"`, and names this module already resolved above.
    """
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _evaluate_path(node.value, env, module_file)
        return None if base is None else base.parent
    if isinstance(node, ast.Call) and not node.keywords:
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "Path" and len(node.args) == 1
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__"):
            return module_file
        if isinstance(func, ast.Attribute) and func.attr == "resolve" and not node.args:
            return _evaluate_path(func.value, env, module_file)
        return None
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        base = _evaluate_path(node.left, env, module_file)
        return None if base is None else base / node.right.value
    return None


def _module_path_constants(module_file: Path, source: str) -> tuple[dict, set]:
    """Module-level ALL-CAPS names -> resolved absolute path; plus the ones that do not resolve."""
    env: dict = {}
    opaque: set = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id.isupper()):
            continue
        resolved = _evaluate_path(value, env, module_file)
        if resolved is None:
            opaque.add(target.id)
            env.pop(target.id, None)
        else:
            env[target.id] = resolved
            opaque.discard(target.id)
    return env, opaque


def _path_parity(sources: dict, repo_root: Path) -> tuple[dict, dict, set]:
    """`{module file: source}` -> (same name different file, uncomparable, shared names).

    Same name different file: `{name: {module: relative path}}`. Uncomparable: one module
    resolves the name, another defines it in a shape that does not resolve -- that copy would
    silently drop out of the comparison, so it is reported rather than skipped.
    """
    def _label(path: Path) -> str:
        return path.relative_to(repo_root).as_posix()

    resolved_by_name: dict = {}
    opaque_by_name: dict = {}
    for module_file, source in sources.items():
        resolved, opaque = _module_path_constants(module_file, source)
        for name, value in resolved.items():
            try:
                shown = value.relative_to(repo_root).as_posix()
            except ValueError:
                shown = str(value)
            resolved_by_name.setdefault(name, {})[_label(module_file)] = shown
        for name in opaque:
            opaque_by_name.setdefault(name, []).append(_label(module_file))
    shared = {name for name, where in resolved_by_name.items() if len(where) > 1}
    disagreements = {name: resolved_by_name[name] for name in sorted(shared)
                     if len(set(resolved_by_name[name].values())) > 1}
    uncomparable = {name: sorted(opaque_by_name[name])
                    for name in sorted(set(resolved_by_name) & set(opaque_by_name))}
    return disagreements, uncomparable, shared


def _unexplained_findings(disagreements: dict, uncomparable: dict, exempt) -> tuple[dict, dict]:
    """Drop the exempt names from both findings -- a deliberately different file can land in either."""
    return ({name: where for name, where in disagreements.items() if name not in exempt},
            {name: where for name, where in uncomparable.items() if name not in exempt})


def _stale_exemptions(disagreements: dict, uncomparable: dict, exempt) -> list:
    """An exempt name that now resolves to one file (or is no longer shared) is stale."""
    return sorted(name for name in exempt if name not in disagreements and name not in uncomparable)


def _real_path_parity() -> tuple[dict, dict, set]:
    return _path_parity({path.resolve(): path.read_text(encoding="utf-8") for path in _SOURCES},
                        PKG_ROOT.parent.resolve())


def test_a_path_constant_names_the_same_file_in_every_process():
    disagreements, uncomparable, shared = _real_path_parity()
    # Positive control: finding no shared name at all looks exactly like "all consistent".
    assert len(shared) >= _PATH_PARITY_FLOOR, (
        f"only {len(shared)} path constants shared across modules -- the extraction is broken, "
        "or the scan scope shrank.")
    assert "WEBRUNNER_PAUSE_FILE" in shared, "the bot and the batch both define the pause marker"
    unexpected, opaque = _unexplained_findings(disagreements, uncomparable,
                                               _SAME_NAME_DIFFERENT_FILE)
    assert not unexpected, (
        f"one constant names different files in different processes: {unexpected}. Both sides "
        "keep running and simply never read what the other wrote. If they really are two files, "
        "rename one, or add it to `_SAME_NAME_DIFFERENT_FILE` with its reason.")
    assert not opaque, (
        f"these constants resolve in one module but are written in an uncomparable shape in "
        f"another: {opaque}. That copy silently drops out of the comparison; write it back as "
        "`PROJECT_ROOT / \"...\"`.")


def test_every_same_name_exemption_still_names_two_different_files():
    disagreements, uncomparable, _shared = _real_path_parity()
    assert _SAME_NAME_DIFFERENT_FILE, "the exemption list is empty -- delete this test instead"
    for name, reason in _SAME_NAME_DIFFERENT_FILE.items():
        assert reason.strip(), f"the exemption for `{name}` has no reason"
    stale = _stale_exemptions(disagreements, uncomparable, _SAME_NAME_DIFFERENT_FILE)
    assert not stale, f"these exemptions now name one file (or are no longer shared); delete them: {stale}"


def test_the_path_parity_check_sees_what_it_claims_to():
    """The real data is consistent, so the comparison needs its own control, or deleting it
    stays green.

    One case per direction: a real difference is reported; a different spelling of the same
    file must **not** be (only this input kills a mutant that compares spellings instead of
    resolved paths); an unresolvable copy is reported as uncomparable. The two exemption helpers
    are checked here too: the real data has no exempt name on the uncomparable side, so a
    mutant filtering only one side is killed only by synthetic input.
    """
    repo = PKG_ROOT.parent.resolve()
    pkg_module = repo / "axiomatic" / "a.py"
    root_module = repo / "b.py"
    other_pkg_module = repo / "axiomatic" / "c.py"
    pkg_root = "PROJECT_ROOT = Path(__file__).resolve().parent.parent\n"
    sources = {
        pkg_module: pkg_root + (
            'X_FILE = PROJECT_ROOT / "x.json"\n'
            'Y_FILE = PROJECT_ROOT / "d" / "y.txt"\n'
            'Z_FILE = PROJECT_ROOT / "z.txt"\n'),
        root_module: "REPO_ROOT = Path(__file__).resolve().parent\n" + (
            'X_FILE = REPO_ROOT / "x2.json"\n'
            'Y_FILE = REPO_ROOT / "d" / "y.txt"\n'),
        other_pkg_module: pkg_root + 'Z_FILE = os.path.join(PROJECT_ROOT, "z.txt")\n',
    }
    disagreements, uncomparable, shared = _path_parity(sources, repo)
    assert disagreements == {"X_FILE": {"axiomatic/a.py": "x.json", "b.py": "x2.json"}}
    assert shared == {"PROJECT_ROOT", "X_FILE", "Y_FILE"}, shared
    assert uncomparable == {"Z_FILE": ["axiomatic/c.py"]}
    assert _unexplained_findings(disagreements, uncomparable, {"Z_FILE": "r"}) == (disagreements, {})
    assert _unexplained_findings(disagreements, uncomparable, {"X_FILE": "r"}) == ({}, uncomparable)
    assert _stale_exemptions(disagreements, uncomparable,
                             {"X_FILE": "r", "Y_FILE": "r", "Z_FILE": "r", "GONE": "r"}) == ["GONE", "Y_FILE"]
