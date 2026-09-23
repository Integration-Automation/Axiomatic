"""收尾路徑上，`kill()` 要排在第一個 `await` **之前**。

這條規則在樹上被寫了三次、實作了四次，而**沒有任何東西在檢查它**。原文（
`dorossi_backend._dorossi_via_claude_code`）：

> 非預期離開（…外面打進來的 `CancelledError`）：**同步**先把行程砍掉，不要等下面
> `_dorossi_reap_proc` 那個有上限的 wait。理由是取消路徑上我們不保證還有機會跑完
> 任何 await——`finally` 裡的等待是盡力而為，`proc.kill()` 不是 coroutine，一定
> 跑得完。

為什麼重要：被取消的協程在 `finally` 裡再 `await` 一次時，如果取消又來一次（逾時、
關機、abort 連打），那個 await 不保證跑得完。後端 CLI 於是**留在主機上繼續跑**
——`dorossi_cc_tools="full"` 模式下它握著一個 shell——而呼叫端還握著那個 session
的鎖，之後每一輪都在排隊等一個永遠不會結束的回合。`proc.kill()` 是同步呼叫，
排在任何 await 之前就一定跑得完，所以順序本身就是那道保險。

這條規則**特別容易被順手整理掉**：`_dorossi_reap_proc` 自己就會殺（它是「先等再
殺」），所以前面那個同步的 kill 看起來像重複。兩處註解都留了「不要挪進去」的警語
——而警語不是執行力。這支把它變成執行力。

⚠️ 這支**不是** `test_dorossi_round_outcome` 那道守門的重複。那一支問的是「kill
丟 `ProcessLookupError` 時接不接得住」，這一支問的是「kill 排在哪裡」。同一行程式
碼的兩個不同性質。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PACKAGE_ROOT.parent
sys.path.insert(0, str(PACKAGE_ROOT))


def _project_sources() -> list[Path]:
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在
    # 套件裡、在範圍內，搬家之後照舊。
    found = [p for p in (*sorted(PACKAGE_ROOT.glob("*.py")),
                         *sorted(Path(__file__).resolve().parent.glob("*.py")))
             if not p.name.startswith(("test_", "_test_"))]
    found += sorted(REPO_ROOT.glob("*.py"))
    return found


def _trees() -> list[tuple[Path, ast.Module]]:
    out = []
    for path in _project_sources():
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"),
                                        str(path))))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
    return out


# ---------------------------------------------------------------------------
# 推導：誰算「收屍的」、誰算「同步砍行程的」
# ---------------------------------------------------------------------------

def _awaits_wait_on_a_parameter(fn: ast.AST) -> bool:
    """這支函式會對它的某個參數 `await`（或排程）`.wait()` 嗎？

    `_dorossi_reap_proc` 是 `asyncio.ensure_future(proc.wait())` 而不是直接 await，
    兩種都算——重點是「它負責等那個行程結束」。
    """
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.posonlyargs}
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait" and not node.args
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in params):
            return True
    return False


def _sync_killers(trees) -> set[str]:
    """**非** async 的函式，body 裡對某個參數呼叫 `.kill()`。

    `verify_dorossi_cli._kill` 就是這一類：它是同步的，所以在 await 之前呼叫它
    與直接寫 `proc.kill()` 是同一件事。
    """
    found: set[str] = set()
    for _path, tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):       # 刻意不收 async def
                continue
            params = {a.arg for a in fn.args.args}
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "kill" and not node.args
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in params):
                    found.add(fn.name)
                    break
    return found


def _direct_reapers(trees) -> dict[str, ast.AsyncFunctionDef]:
    """直接收屍：async、而且自己會等某個參數行程結束。

    這一類**先等再殺**是刻意的（`_dorossi_reap_proc` 的 docstring 寫了理由：正常
    路徑上讓行程自己好好離開，零額外延遲），所以順序規則不適用於它們自己——適用的
    是呼叫它們的那一層。
    """
    direct: dict[str, ast.AsyncFunctionDef] = {}
    for _path, tree in trees:
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef) and _awaits_wait_on_a_parameter(fn):
                direct[fn.name] = fn
    return direct


def _reapers(trees) -> dict[str, ast.AsyncFunctionDef]:
    """收屍函式：直接收屍的，加上一跳轉交的（收屍的呼叫收屍的）。"""
    direct = _direct_reapers(trees)
    all_async: dict[str, ast.AsyncFunctionDef] = {}
    for _path, tree in trees:
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                all_async[fn.name] = fn
    # 一跳：`_reap_usage_query_proc` 自己不 await `wait()`，它 await 直接收屍的那支。
    #
    # ⚠️ 一跳要收得很緊，不然會整個垮掉。第一版只問「有沒有 await 到一支直接收屍
    # 的函式」，於是 `_dorossi_via_codex`、`probe_smtc_raw_async` 這些**被守的對象
    # 自己**全被算成收屍函式（實測 10 支，而真正的只有 3 支）。多出來的今天沒有造成
    # 誤判，但它會讓 `len(reapers) >= 2` 那道下限變成恆真，也遲早會生出假的收尾點。
    # 兩個條件把它收回來：**自己不 spawn 子行程**（spawn 的是被守的那一方），而且
    # **傳給收屍函式的是自己的參數**（轉交別人的行程，不是自己的）。
    hopped = dict(direct)
    for name, fn in all_async.items():
        if name in direct or _spawned_process_names(fn):
            continue
        params = {a.arg for a in fn.args.args}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) in direct
                    and any(isinstance(a, ast.Name) and a.id in params
                            for a in node.value.args)):
                hopped[name] = fn
                break
    return hopped


def _spawned_process_names(fn: ast.AST) -> set[str]:
    """這支函式裡被綁到 `asyncio.create_subprocess_*` 的名字。"""
    names: set[str] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr.startswith("create_subprocess")):
            continue
        if isinstance(node.targets[0], ast.Name):
            names.add(node.targets[0].id)
    return names


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _kills_before_first_await(body: list[ast.stmt], procs: set[str],
                              killers: set[str]) -> bool:
    """這一串語句裡，同步砍行程有沒有排在第一個 `await` 之前？

    「同步砍」有兩種寫法：`proc.kill()`，或呼叫一支同步的、會砍那個行程的 helper。
    """
    first_await = None
    kill_lines: list[int] = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Await):
                if first_await is None or node.lineno < first_await:
                    first_await = node.lineno
            elif isinstance(node, ast.Call):
                hits_proc = any(isinstance(a, ast.Name) and a.id in procs
                                for a in node.args)
                is_method_kill = (isinstance(node.func, ast.Attribute)
                                  and node.func.attr == "kill"
                                  and not node.args
                                  and isinstance(node.func.value, ast.Name)
                                  and node.func.value.id in procs)
                if is_method_kill or (hits_proc
                                      and _call_name(node) in killers):
                    kill_lines.append(node.lineno)
    if not kill_lines:
        return False
    if first_await is None:
        return True
    return min(kill_lines) < first_await


def _catches_base_exception(handler: ast.ExceptHandler) -> bool:
    """只有接得住 `BaseException` 的 handler 才涵蓋取消路徑。

    `CancelledError` 是 `BaseException` 的直系，任何 `except Exception` 都接不到
    ——而取消正是這條規則要防的那一種離開方式。
    """
    if handler.type is None:
        return True                       # bare except
    names = []
    target = handler.type
    if isinstance(target, ast.Tuple):
        names = [getattr(e, "id", "") for e in target.elts]
    else:
        names = [getattr(target, "id", "")]
    return any(n in ("BaseException", "CancelledError") for n in names)


def _cleanup_sites(trees, reapers, killers):
    """回 `[(檔名, 函式名, 行號, 符合的寫法或 None)]`。

    納入範圍的條件：這支函式**自己** spawn 了 asyncio 子行程，而且在某處 `await`
    一支收屍函式去收它。參數傳進來的行程不在範圍內（推導看不到它是誰 spawn 的），
    那類由 `_INDIRECT_CLEANUP_SITES` 具名列管。
    """
    sites = []
    for path, tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            procs = _spawned_process_names(fn)
            if not procs:
                continue
            reap_lines = [
                node.lineno for node in ast.walk(fn)
                if isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and _call_name(node.value) in reapers
                and any(isinstance(a, ast.Name) and a.id in procs
                        for a in node.value.args)
            ]
            if not reap_lines:
                continue
            sites.append((path.name, fn.name, min(reap_lines),
                          _compliant_shape(fn, procs, reapers, killers)))
    return sites


def _compliant_shape(fn, procs, reapers, killers) -> str | None:
    """四種被接受的寫法，回哪一種（都不符合回 None）。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        # (a) `finally` 裡，同步 kill 排在第一個 await 之前
        if node.finalbody and _kills_before_first_await(
                node.finalbody, procs, killers):
            return "finally 裡先砍再等"
        # (b) 接得住 BaseException 的 handler 裡，同步 kill 排在任何 await 之前
        for handler in node.handlers:
            if (_catches_base_exception(handler)
                    and _kills_before_first_await(handler.body, procs, killers)):
                return "except BaseException 裡先砍"
    # (c) 收屍函式自己在第一個 await 之前就同步砍了
    for node in ast.walk(fn):
        if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                and _call_name(node.value) in reapers):
            reaper = reapers[_call_name(node.value)]
            params = {a.arg for a in reaper.args.args}
            if _kills_before_first_await(reaper.body, params, killers):
                return "收屍函式自己先砍"
    return None


# （本檔不再需要具名清單：推導看不見的那個形狀與「轉交型收屍函式」結構上是同一個，
#   所以直接對那一層套同一條順序規則，見 `test_a_delegating_reaper_kills_before_it_delegates`。）


# ---------------------------------------------------------------------------
# 守門
# ---------------------------------------------------------------------------

def test_every_cleanup_path_kills_before_it_awaits():
    """每一個「自己 spawn、自己收屍」的函式都要先同步砍、再 await。"""
    trees = _trees()
    reapers = _reapers(trees)
    killers = _sync_killers(trees)
    sites = _cleanup_sites(trees, reapers, killers)
    assert len(sites) >= 4, (
        f"只推導出 {len(sites)} 個收尾點（{sites}）——推導失效時這支會變成空的比較，"
        "看起來跟通過一模一樣。")
    bad = [f"{name}:{fn}()（第 {line} 行的收屍之前沒有同步的 kill）"
           for name, fn, line, shape in sites if shape is None]
    assert not bad, (
        "這些收尾路徑在 await 收屍之前沒有同步砍掉行程：\n  " + "\n  ".join(bad)
        + "\n取消路徑上那個 await 不保證跑得完，後端行程會留在主機上繼續跑，"
          "而呼叫端還握著 session 的鎖。`proc.kill()` 是同步呼叫，排在任何 await "
          "之前就一定跑得完。")


def test_the_derivation_finds_the_reapers_and_the_sync_killers():
    """兩份推導出來的集合都不得是空的——空集合會讓上面那支無聲放寬。

    `_reapers` 空掉 → 一個收尾點都認不出來 → 比較變成空的；
    `_sync_killers` 空掉 → 用 helper 砍的那幾處會被誤判成違規（叫狼來了）。
    """
    trees = _trees()
    reapers = _reapers(trees)
    killers = _sync_killers(trees)
    assert "_dorossi_reap_proc" in reapers, (
        f"推導不出主要的收屍函式，只找到 {sorted(reapers)}")
    assert len(reapers) >= 2, f"收屍函式只推導出 {sorted(reapers)}"
    assert killers, "一支同步砍行程的 helper 都沒推導出來"


def test_all_four_accepted_shapes_are_actually_in_use():
    """四種寫法不是憑空列的——樹上真的各有人在用。

    這一格的用途是**反向**的：哪天某一種寫法在樹上絕跡了，接受它的那段程式碼就
    變成永遠不會執行的死碼，而死碼是下一個人「順手簡化」的第一個目標。真的絕跡
    的時候這支會紅，那時該做的是決定「要不要繼續接受這種寫法」，不是把它靜靜刪掉。
    """
    trees = _trees()
    reapers = _reapers(trees)
    killers = _sync_killers(trees)
    shapes = {shape for _n, _f, _l, shape in
              _cleanup_sites(trees, reapers, killers) if shape}
    assert shapes >= {"finally 裡先砍再等", "except BaseException 裡先砍",
                      "收屍函式自己先砍"}, (
        f"樹上目前只用到這幾種寫法：{sorted(shapes)}")


def _delegating_reapers(trees) -> dict[str, ast.AsyncFunctionDef]:
    """轉交型收屍函式：自己不等，把行程交給直接收屍的那一支。"""
    direct = _direct_reapers(trees)
    return {name: fn for name, fn in _reapers(trees).items()
            if name not in direct}


def test_a_delegating_reaper_kills_before_it_delegates():
    """轉交型的收屍函式，自己就得先同步砍。

    這一格補的是上面那道推導**結構上看不到**的東西：行程從參數傳進來時，推導不
    知道是誰 spawn 的。與其為那個盲區養一份具名清單（空清單會讓守門自己解除武裝，
    本 repo 記過），不如把同一條順序規則直接套在這一層——因為盲區裡的函式與轉交
    型收屍函式**是同一個形狀**，結構上分不開，而它們要遵守的規則也一樣。

    直接收屍的那一類刻意排除在外：`_dorossi_reap_proc` 的「先等再殺」是寫了理由的
    設計（正常路徑上讓行程自己好好離開），順序規則要管的是呼叫它的那一層。
    """
    trees = _trees()
    killers = _sync_killers(trees)
    delegating = _delegating_reapers(trees)
    assert delegating, (
        "一支轉交型收屍函式都沒推導出來——這支測試會變成空的比較。"
        "真的都不見了的話，把它跟這段說明一起拿掉，不要留著。")
    bad = [name for name, fn in delegating.items()
           if not _kills_before_first_await(
               fn.body, {a.arg for a in fn.args.args}, killers)]
    assert not bad, (
        f"這些收屍函式把行程轉交出去之前沒有先同步砍：{bad}。"
        "呼叫它們的地方多半是收尾／取消路徑，而那裡不保證還有機會跑完任何 await。")


def test_the_delegating_reaper_check_actually_fires():
    """對照組：一支轉交出去、但自己沒先砍的收屍函式要被抓到。"""
    source = '''
async def _reap(proc):
    await proc.wait()

async def _hands_it_over(proc):
    await _reap(proc)
'''
    trees = [(Path("synthetic.py"), ast.parse(source))]
    delegating = _delegating_reapers(trees)
    assert "_hands_it_over" in delegating, (
        f"合成語料裡的轉交型收屍函式沒被認出來：{sorted(delegating)}")
    fn = delegating["_hands_it_over"]
    assert not _kills_before_first_await(
        fn.body, {a.arg for a in fn.args.args}, _sync_killers(trees))


def test_the_delegating_reaper_check_passes_a_correct_one():
    """近似反例：同樣的形狀、但先砍了，就要放行。

    少了這一格，「一律回報違規」這個變異會活下來。
    """
    source = '''
async def _reap(proc):
    await proc.wait()

async def _kills_first(proc):
    proc.kill()
    await _reap(proc)
'''
    trees = [(Path("synthetic.py"), ast.parse(source))]
    fn = _delegating_reapers(trees)["_kills_first"]
    assert _kills_before_first_await(
        fn.body, {a.arg for a in fn.args.args}, _sync_killers(trees))


# ---------------------------------------------------------------------------
# 對照組——樹是乾淨的，所以「回報違規」那幾行在真實資料上一次都不會執行
# ---------------------------------------------------------------------------

_COMPLIANT = '''
async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    finally:
        proc.kill()
        await _reap(proc)
'''

_KILL_AFTER_THE_AWAIT = '''
async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    finally:
        await _reap(proc)
        proc.kill()
'''

_NO_KILL_AT_ALL = '''
async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    finally:
        await _reap(proc)
'''

_ONLY_CATCHES_EXCEPTION = '''
async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    except Exception:
        proc.kill()
        raise
    finally:
        await _reap(proc)
'''

_VIA_A_SYNC_HELPER = '''
def _kill(proc):
    proc.kill()

async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    except BaseException:
        _kill(proc)
        raise
    finally:
        await _reap(proc)
'''

_CONTROLS = [
    ("合規：finally 裡先砍再等", _COMPLIANT, True),
    ("kill 排在 await 後面", _KILL_AFTER_THE_AWAIT, False),
    ("完全沒有同步的 kill", _NO_KILL_AT_ALL, False),
    ("只接得住 Exception（接不到取消）", _ONLY_CATCHES_EXCEPTION, False),
    ("合規：透過同步 helper 砍", _VIA_A_SYNC_HELPER, True),
]


@pytest.mark.parametrize("label,source,expected",
                         _CONTROLS, ids=[row[0] for row in _CONTROLS])
def test_the_detection_works_on_a_synthetic_corpus(label, source, expected):
    """判斷本身要在合成語料上開火。

    三個必須抓到的破綻各有各的機制（順序、缺席、handler 型別太窄），兩個必須放行
    的則是**近似反例**——沒有它們，「一律回報違規」這個變異會活下來。
    """
    tree = ast.parse(source)
    trees = [(Path("synthetic.py"), tree)]
    reapers = _reapers(trees)
    killers = _sync_killers(trees)
    assert "_reap" in reapers, "合成語料裡的收屍函式沒被認出來，這一格等於沒測"
    sites = _cleanup_sites(trees, reapers, killers)
    assert len(sites) == 1, f"合成語料應該剛好一個收尾點，拿到 {sites}"
    shape = sites[0][3]
    assert (shape is not None) is expected, (
        f"「{label}」判成 {shape!r}，預期 {'合規' if expected else '違規'}")


def test_a_cancellation_only_handler_is_not_a_substitute_for_the_order():
    """近似反例：`except BaseException` 有了，但 kill 排在它自己的 await 後面。

    沒有這一格的話，「只要有 BaseException handler 就放行」這個放寬版本會全綠。
    """
    source = '''
async def _reap(proc):
    await proc.wait()

async def spawner():
    proc = await asyncio.create_subprocess_exec("x")
    try:
        await proc.stdout.readline()
    except BaseException:
        await _reap(proc)
        proc.kill()
        raise
    finally:
        pass
'''
    trees = [(Path("synthetic.py"), ast.parse(source))]
    reapers = _reapers(trees)
    sites = _cleanup_sites(trees, reapers, _sync_killers(trees))
    assert len(sites) == 1
    assert sites[0][3] is None, (
        "handler 接得住取消，但 kill 排在自己的 await 後面——順序才是這條規則的內容")
