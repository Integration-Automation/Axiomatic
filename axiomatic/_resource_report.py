"""「這一套現在花掉這台主機多少」——依角色分組的行程數與資源占用（bot 側）。

`/proc usage` 的資料來源。回答的是**主機這一側**的問題：本專案此刻在這台機器上
開著幾個行程、分別是什麼角色、吃掉多少 CPU 與記憶體、最久的那個跑了多久，以及
這些數字對整台機器來說是多少（記憶體剩多少、產出那顆磁碟剩多少）。

**刻意不回答的事，因為已經有人回答了。** 批次做到哪一對、還剩幾張，是
`/gen current` ／ `/gen progress`；對話助理此刻有哪幾輪在跑、在等空位還是在跑，是
`/dorossi running`。那兩份讀的是**進度與工作佇列**，本模組讀的是**作業系統的行程
表**——同一件事由兩個地方各講一半是本專案最常見的漂移來源，所以這裡只講主機側，
並在回覆的最後一行指過去。反過來說，那兩份完全看不到瀏覽器吃了 4 GB、也看不到
批次已經連續跑了十四小時，那正是本模組存在的理由。

模組邊界（`CLAUDE.md` 硬規則）：這是 **bot 側專屬模組**，不得 `import discord_bot`
（循環），也不得 `from webrunner_novelai import …`。它只 import stdlib、選用的
psutil，以及同為 bot 側的 `_process_control`——而且**只讀**：用的是那邊的判定述詞
（`looks_like_python_process`／`cmdline_runs_script`／`collapse_interpreter_stub_pairs`）
與腳本名常數，一個字都不改。理由是那個模組在批次的 import 閉包裡
（`mutation_harness.batch_modules_at_risk()`），改它等於改一支正在跑的批次。

不洩漏規則：本模組**不組任何要送出去的句子**。它回的是數字與角色代號，PID、磁碟
路徑要不要露出去由 `discord_bot._owner_detail()` 那個單一決策點決定（與
`/dorossi running` 同一個分工）。本模組自己往 stderr 印的診斷一律 ASCII。

**為什麼自己掃一遍，而不是叫 `_process_control` 那四支掃描器。** 那四支
（`_find_all_webrunner_pids`／`_find_all_chrome_processes`／`find_launcher_pids`×2）
各自跑一趟完整的行程表列舉，而且回的是 `(pid, 名稱)`——一個資源數字都沒有，所以
呼叫它們之後仍然要再走一趟去讀 RSS／CPU／啟動時間。四趟列舉換不到任何東西。所以
這裡走一趟，而把**判定**（哪個命令列算是在跑那支腳本、轉接殼怎麼併）留給那個模組
的純述詞——會漂移的是判定，不是迴圈。
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import time

try:                                    # 直接跑腳本（本機開發）走這一條
    import _process_control as _pc
except ImportError:                     # 走套件路徑（`from axiomatic import …`）
    from axiomatic import _process_control as _pc


# ---------- 角色 -----------------------------------------------------------
#
# 順序就是回覆裡的順序：由「這一套的本體」往外走到「它生出來的東西」。

ROLE_BOT = "bot"
ROLE_LAUNCHER = "launcher"
ROLE_BATCH = "batch"
ROLE_BROWSER = "browser"
ROLE_ASSISTANT = "assistant"
ROLE_CHILD = "child"

ROLE_ORDER = (ROLE_BOT, ROLE_LAUNCHER, ROLE_BATCH, ROLE_BROWSER,
              ROLE_ASSISTANT, ROLE_CHILD)

# 泛用標籤，與 `/sys health` 既有的用字一致（「背景產圖程式」等）。
ROLE_LABELS = {
    ROLE_BOT: "對話程式",
    ROLE_LAUNCHER: "啟動器／監督者",
    ROLE_BATCH: "背景產圖程式",
    ROLE_BROWSER: "瀏覽器與驅動程式",
    ROLE_ASSISTANT: "對話助理的後端",
    ROLE_CHILD: "其他子行程",
}

# 角色判定：腳本檔名 → 角色。命中條件是「這個 Python 行程**在執行**這支腳本」，
# 用 `_process_control.cmdline_runs_script` 的判定，不是 cmdline 子字串比對——
# 理由見該函式（`python -m black webrunner_novelai.py` 不是在跑批次）。
SCRIPT_ROLES = {
    "discord_bot.py": ROLE_BOT,
    "start_discord_bot.py": ROLE_LAUNCHER,
    _pc.LAUNCHER_SCRIPT_NAME: ROLE_LAUNCHER,   # start_webrunner.py
    "run_batch.py": ROLE_LAUNCHER,
    "webrunner_novelai.py": ROLE_BATCH,
    "webrunner_je_only.py": ROLE_BATCH,
}

# 名稱就足以判定的角色。**所有** chrome／chromedriver 都算進來，與
# `_process_control._find_all_chrome_processes` 同一個判準：那支的註解寫得很清楚，
# Chrome 的 renderer／GPU／utility 子行程不一定帶得到 `--user-data-dir`，被殺掉的
# 父行程底下的子行程還會被改掛到系統行程下面，所以「靠家譜分辨哪幾個是我們的」
# 在這裡辦不到。這一套本來就是「執行前殺光所有 chrome」，所以**會被這一套殺掉的
# 那一群，就是這一行要數的那一群**——兩邊同一個定義才不會出現「清單說 24 個、
# sweep 殺 30 個」。回覆會把這個前提講出來。
BROWSER_NAMES = ("chrome.exe", "chromedriver.exe")

# 家譜往上走的步數上限。行程表是別的行程在改的活資料，`ppid` 有機會指到一個已經
# 被回收再發出去的號碼而繞成環；有上限就不會卡住，代價只是極深的樹判不出祖先。
_ANCESTOR_MAX_DEPTH = 12

# CPU 取樣的預設區間（秒）。psutil 的 `cpu_percent` 是兩次取樣的差，所以一定要有
# 一段真實的牆鐘時間；太短的話數字全是雜訊，太長的話指令變慢。本機實測見
# `architecture.md`。**這段時間裡整支 helper 都在睡**，所以呼叫端
# 必須用 `asyncio.to_thread` 丟出事件迴圈（本專案的既有慣例）。
DEFAULT_CPU_INTERVAL_SEC = 0.4


# ---------- 資料形狀 -------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProcRow:
    """一個行程在這份報告裡的樣子。取不到的欄位是 `None`，**不是 0**。

    這個區別是承重的：0 會被加進總和，然後那個總和會被當成事實講出去。"""
    pid: int
    name: str = ""
    ppid: int | None = None
    script: str | None = None       # 正在執行的腳本檔名（非 Python → None）
    started: float | None = None    # epoch 秒
    rss: int | None = None
    cpu: float | None = None        # psutil 的「一顆核心 ＝ 100%」口徑


@dataclasses.dataclass(frozen=True)
class RoleUsage:
    """一個角色的合計。"""
    role: str
    procs: int = 0            # 作業系統行程數（轉接殼也算，它真的佔記憶體）
    logical: int = 0          # 併掉轉接殼之後的「邏輯實例數」
    rss: int | None = None    # 位元組；一個都讀不到時 None
    cpu: float | None = None  # 同上
    oldest_started: float | None = None   # 最早啟動的那一個（epoch）
    missing_rss: int = 0      # 讀不到 RSS 的行程數
    missing_cpu: int = 0
    top: tuple = ()           # 依 RSS 由大到小的前幾筆 `ProcRow`


@dataclasses.dataclass(frozen=True)
class ScanResult:
    """一次行程表掃描的原始結果。注入用的形狀——測試餵一個這個進來就能跑完整段
    判定，不必依賴這台機器真正的行程表（那種測試在別人的機器上、在批次沒跑的時候
    會得到不同的答案，遲早被標成 skip）。"""
    rows: tuple = ()
    scan_ok: bool = True
    notes: tuple = ()
    system_cpu: float | None = None
    descendant_roots: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """一次量測的全部結果。任何一格都可能缺，缺了就是 `None` ＋ 一筆 note。"""
    roles: dict = dataclasses.field(default_factory=dict)
    notes: tuple = ()         # `(鍵, 細節)`，由呼叫端翻成句子
    scan_ok: bool = True
    taken_at: float = 0.0
    elapsed: float = 0.0      # 這次量測自己花掉的秒數
    cpu_interval: float = DEFAULT_CPU_INTERVAL_SEC
    # 整機
    cpu_count: int | None = None
    cpu_percent: float | None = None
    mem_total: int | None = None
    mem_available: int | None = None
    disk_total: int | None = None
    disk_free: int | None = None
    disk_path: str = ""

    @property
    def total_procs(self) -> int:
        return sum(usage.procs for usage in self.roles.values())


# ---------- 純判定：分類 ---------------------------------------------------

def role_of_row(row: ProcRow) -> str | None:
    """只看這一列自己就判得出來的角色；判不出來回 `None`（留給家譜那一步）。"""
    name = (row.name or "").strip().lower()
    if name in BROWSER_NAMES:
        return ROLE_BROWSER
    if row.script:
        return SCRIPT_ROLES.get(row.script)
    return None


def classify(rows, *, assistant_pids=(), own_pid: int | None = None,
             descendant_roots=None) -> dict:
    """`{pid: 角色}`。不是本專案的行程不會出現在結果裡。

    兩段式，順序是承重的：

    1. **自己就判得出來的**先判（腳本名、`chrome.exe`）。批次是 bot 生的、瀏覽器
       是批次生的，所以它們同時也是「某個本專案行程的後代」——先判這一段，才不會
       把整棵瀏覽器算成「其他子行程」。
    2. 剩下的找祖先。先看 `descendant_roots`（`{pid: 它是誰的後代}`，掃描時由
       `children(recursive=True)` 一次算出來），沒有才往上走 `ppid` 鏈。祖先是
       `assistant_pids` 裡的某一個（bot 自己登記的後端子行程 pid）就是
       `assistant`，是其他已分類行程就是 `child`，都不是就不是我們的東西，不列入。

    **為什麼有兩條路。** `ppid` 鏈是給測試與注入資料用的直白寫法，但在 Windows 上
    每問一次 `ppid` psutil 都要重建一次**整台機器**的對照表（本機實測每次約 21
    毫秒，83 個候選就是 1.7 秒——這個 repo 已經為同一個陷阱寫過一張量測表，見
    `_process_control._find_all_webrunner_pids`）。`children(recursive=True)` 一次
    呼叫只重建一次，所以正式路徑走那一條，`ppid` 只對需要併轉接殼的那幾筆問。

    `own_pid` 無條件是 `bot`：問這個問題的行程就是 bot 自己，這比任何命令列比對都
    確定，而命令列比對在「被包成單一執行檔」「用 `-m` 啟動」這兩種形狀下都會失手
    ——失手的樣子是報告裡沒有 bot 那一行，看起來像 bot 沒在跑。

    `assistant_pids` 由呼叫端提供而**不是**這裡去猜行程名：後端 CLI 在這台機器上
    的行程名（node／殼層轉接）會隨安裝方式變，猜錯的兩個方向都難看——猜寬了會把
    使用者自己的編輯器算進來，猜窄了會讓這一格永遠是 0 而看起來像「沒在跑」。
    bot 手上本來就有那些 pid（每一輪都登記著），用已知的事實不要用猜的。
    """
    by_pid = {row.pid: row for row in rows}
    parent = {row.pid: row.ppid for row in rows}
    roles: dict[int, str] = {}
    for row in rows:
        role = role_of_row(row)
        if role:
            roles[row.pid] = role
    if isinstance(own_pid, int) and own_pid in by_pid:
        roles[own_pid] = ROLE_BOT

    wanted = {pid for pid in assistant_pids if isinstance(pid, int)}
    # 被登記的後端子行程自己：即使它同時長得像別的東西，這個身分優先——它是 bot
    # 親手生的，bot 比任何名稱比對都確定。
    for pid in wanted:
        if pid in by_pid:
            roles[pid] = ROLE_ASSISTANT

    def _from_ancestor(ancestor_pid) -> str | None:
        if ancestor_pid in wanted:
            return ROLE_ASSISTANT
        ancestor = roles.get(ancestor_pid)
        if not ancestor:
            return None
        return ROLE_ASSISTANT if ancestor == ROLE_ASSISTANT else ROLE_CHILD

    known = descendant_roots or {}
    for row in rows:
        if row.pid in roles:
            continue
        role = _from_ancestor(known.get(row.pid))
        if role:
            roles[row.pid] = role
            continue
        seen = {row.pid}
        cursor = row.ppid
        depth = 0
        while (isinstance(cursor, int) and cursor > 0
               and cursor not in seen and depth < _ANCESTOR_MAX_DEPTH):
            role = _from_ancestor(cursor)
            if role:
                roles[row.pid] = role
                break
            seen.add(cursor)
            cursor = parent.get(cursor)
            depth += 1
    return roles


def _sum_or_none(values):
    """有幾個算幾個；**一個都沒有才回 `None`**。回 `(合計, 缺了幾個)`。"""
    got = [v for v in values if isinstance(v, (int, float))
           and not isinstance(v, bool)]
    missing = len(values) - len(got)
    return (sum(got) if got else None), missing


def summarise(rows, roles, *, top_n: int = 3) -> dict:
    """把分類結果合計成每個角色一筆 `RoleUsage`。純函式。"""
    buckets: dict[str, list] = {}
    for row in rows:
        role = roles.get(row.pid)
        if role:
            buckets.setdefault(role, []).append(row)

    out: dict[str, RoleUsage] = {}
    for role in ROLE_ORDER:
        members = buckets.get(role) or []
        if not members:
            continue
        rss, missing_rss = _sum_or_none([m.rss for m in members])
        cpu, missing_cpu = _sum_or_none([m.cpu for m in members])
        starts = [m.started for m in members
                  if isinstance(m.started, (int, float))
                  and not isinstance(m.started, bool) and m.started > 0]
        # **併轉接殼只對 Python 那三個角色做。** `collapse_interpreter_stub_pairs`
        # 的規則是「父行程也在名單裡就丟掉」，那對「venv 轉接殼 ＋ 本尊」剛好對，
        # 但瀏覽器是一整棵同名的父子樹（主行程 ＋ N 個 renderer／GPU／utility），
        # 同一條規則會把 24 個 chrome 併成 1 個，然後報告會說瀏覽器只開了一個。
        if role in (ROLE_BOT, ROLE_LAUNCHER, ROLE_BATCH):
            logical = len(_pc.collapse_interpreter_stub_pairs(
                [(m.pid, m.ppid or 0, m.script or m.name) for m in members]))
        else:
            logical = len(members)
        top = tuple(sorted(
            (m for m in members if isinstance(m.rss, int)),
            key=lambda m: m.rss, reverse=True)[:max(0, top_n)])
        out[role] = RoleUsage(
            role=role, procs=len(members), logical=logical,
            rss=rss, cpu=cpu,
            oldest_started=min(starts) if starts else None,
            missing_rss=missing_rss, missing_cpu=missing_cpu, top=top)
    return out


# ---------- 實際去問作業系統 ----------------------------------------------

def _psutil_rows(*, own_pid: int, cpu_interval: float, extra_pids=()):
    """走一趟行程表，回一份 `ScanResult`。**永不 raise。**

    只對**有可能是我們的**行程付昂貴的成本（`ppid` 在 Windows 上每呼叫一次就重建
    一次全系統對照表，見 `_process_control._find_all_webrunner_pids` 的量測表），
    所以流程是「先用最便宜的 `attrs` 過一遍 → 只對命中的那幾個讀細節」。

    `scan_ok=False` 代表**這份名單不完整**，呼叫端必須說出來。「列舉一開始就炸」
    與「機器上真的很乾淨」回的都會是短名單，兩者在畫面上分不出來——這個專案已經
    為同一個形狀付過兩次代價（見那兩支掃描器的 docstring）。單一行程在列舉途中
    消失或權限不足**不算**掃描失敗，那是這個 API 的日常。
    """
    notes: list[tuple] = []
    try:
        import psutil  # type: ignore  # noqa: PLC0415
    except ImportError:
        return ScanResult(rows=(), scan_ok=False,
                          notes=(("psutil_missing", None),))

    # **兩個都按 pid 記，不是按「發生幾次」計數。** 同一個行程在細節那一輪會被問
    # 三次（啟動時間、RSS、CPU），權限不足就是三次都不給——用計數的話「3 個行程
    # 沒有權限讀」其實是同一個行程，而那句話是要給人看的。列舉階段拿不到 pid，
    # 所以那一段仍然是計數，兩者最後相加。
    gone_enum = 0
    gone_pids: set = set()
    denied_pids: set = set()
    scan_ok = True
    candidates: dict[int, object] = {}
    scripts: dict[int, str | None] = {}
    names: dict[int, str] = {}
    try:
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if pid <= 0:
                    continue
                name = proc.info.get("name") or ""
                low = name.strip().lower()
                if low in BROWSER_NAMES:
                    candidates[pid] = proc
                    names[pid] = name
                    scripts[pid] = None
                    continue
                if _pc.looks_like_python_process(name):
                    script = _pc.cmdline_runs_script(proc.cmdline(),
                                                     tuple(SCRIPT_ROLES))
                    if script:
                        candidates[pid] = proc
                        names[pid] = name
                        scripts[pid] = script
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                gone_enum += 1
                continue
    except Exception as error:  # pylint: disable=broad-except
        # `!r`：例外的 str 可能帶出路徑（本專案的洩漏規則）。
        print(f"_resource_report scan failed: {error!r}", file=sys.stderr)
        scan_ok = False

    # 後代：一次 `children(recursive=True)` 在 Windows 上只重建一次全系統對照表
    # （psutil 自己就是這樣實作的），比對每個行程各問一次 `ppid` 便宜一個數量級。
    #
    # **順序是承重的**：登記過的後端子行程放在最後掃，所以它底下那一整串（殼層、
    # 它自己又開的工具行程）會把先前寫進去的「bot 的後代」覆蓋掉。不這樣的話整串
    # 都會算成「其他子行程」，而「對話助理的後端花了多少」正是這個指令要回答的問題
    # 之一。
    roots = [pid for pid, script in scripts.items()
             if script and SCRIPT_ROLES.get(script) in
             (ROLE_BOT, ROLE_LAUNCHER, ROLE_BATCH)]
    roots.insert(0, own_pid)
    roots += [p for p in extra_pids if isinstance(p, int)]
    descendant_roots: dict[int, int] = {}
    for root in dict.fromkeys(p for p in roots if isinstance(p, int) and p > 0):
        try:
            kids = psutil.Process(root).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            gone_enum += 1
            continue
        except Exception as error:  # pylint: disable=broad-except
            print(f"_resource_report children failed: {error!r}",
                  file=sys.stderr)
            scan_ok = False
            continue
        # **每個子行程各自 try。** 整批包一個 try 的話，名單中途有一個行程結束
        # （`name()` 丟 `NoSuchProcess`）就會把**這個根底下剩下的全部**一起丟掉，
        # 而行程在列舉途中消失是這個 API 的日常，不是掃描失敗。
        for kid in kids:
            try:
                name = kid.name()
            except Exception:  # pylint: disable=broad-except
                gone_enum += 1
                continue
            descendant_roots[kid.pid] = root
            if kid.pid not in candidates:
                candidates[kid.pid] = kid
                names[kid.pid] = name
                scripts[kid.pid] = None

    # 細節。CPU 要兩次取樣才有數字，所以先全部墊一次、睡一段、再讀一次。
    #
    # **`ppid()` 只問「跑著本專案腳本的 Python 行程」那幾筆**，因為只有它們會遇到
    # virtualenv 轉接殼、需要併成一筆。對全部候選問一遍的代價實測是 1.7 秒（83 筆
    # × 每次重建一份全系統對照表），而那 1.7 秒換到的資訊，`children()` 那一步已經
    # 用一次呼叫拿到了。
    ppids: dict[int, int | None] = {}
    starts: dict[int, float | None] = {}
    rss: dict[int, int | None] = {}
    for pid, proc in list(candidates.items()):
        if scripts.get(pid):
            try:
                ppids[pid] = proc.ppid()
            except Exception:  # pylint: disable=broad-except
                ppids[pid] = None
        try:
            starts[pid] = proc.create_time()
        except Exception:  # pylint: disable=broad-except
            starts[pid] = None
        try:
            rss[pid] = proc.memory_info().rss
        except psutil.AccessDenied:
            rss[pid] = None
            denied_pids.add(pid)
        except Exception:  # pylint: disable=broad-except
            rss[pid] = None
        try:
            proc.cpu_percent(None)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass

    system_cpu = None
    try:
        # 整機那一格順便在這段睡眠裡量完——`cpu_percent(interval=…)` 自己會睡滿
        # 這段時間，另外再 `sleep` 一次等於把指令的成本乘二。
        system_cpu = psutil.cpu_percent(interval=max(0.0, cpu_interval))
    except Exception as error:  # pylint: disable=broad-except
        print(f"_resource_report cpu sample failed: {error!r}", file=sys.stderr)
        notes.append(("cpu_unavailable", None))
        try:
            time.sleep(max(0.0, cpu_interval))
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass

    rows: list[ProcRow] = []
    for pid, proc in candidates.items():
        try:
            cpu = proc.cpu_percent(None)
        except psutil.NoSuchProcess:
            gone_pids.add(pid)
            cpu = None
        except psutil.AccessDenied:
            denied_pids.add(pid)
            cpu = None
        except Exception:  # pylint: disable=broad-except
            cpu = None
        rows.append(ProcRow(
            pid=pid, name=names.get(pid, ""), ppid=ppids.get(pid),
            script=scripts.get(pid), started=starts.get(pid),
            rss=rss.get(pid), cpu=cpu))

    if not scan_ok:
        notes.append(("scan_incomplete", None))
    gone = gone_enum + len(gone_pids)
    if gone:
        notes.append(("gone", gone))
    if denied_pids:
        notes.append(("denied", len(denied_pids)))
    return ScanResult(rows=tuple(rows), scan_ok=scan_ok,
                      notes=tuple(notes), system_cpu=system_cpu,
                      descendant_roots=descendant_roots)


def _machine_facts(disk_path):
    """整機那幾格：核心數、記憶體、`disk_path` 那顆磁碟。回 `(dict, notes)`。

    三件事各自失敗、各自降級成缺一格加一筆 note——把三個問題綁成一個 try 的話，
    一顆查不到的磁碟會連帶讓記憶體那一行消失，而使用者看到的是「沒講」，跟「這台
    機器沒有記憶體壓力」長得一樣。"""
    facts: dict = {"cpu_count": None, "mem_total": None, "mem_available": None,
                   "disk_total": None, "disk_free": None,
                   "disk_path": str(disk_path or "")}
    notes: list[tuple] = []
    try:
        import psutil  # type: ignore  # noqa: PLC0415
    except ImportError:
        notes.append(("memory_unavailable", None))
        psutil = None  # type: ignore
    if psutil is not None:
        try:
            facts["cpu_count"] = psutil.cpu_count(logical=True)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        try:
            virtual = psutil.virtual_memory()
            facts["mem_total"] = int(virtual.total)
            facts["mem_available"] = int(virtual.available)
        except Exception as error:  # pylint: disable=broad-except
            print(f"_resource_report virtual_memory failed: {error!r}",
                  file=sys.stderr)
            notes.append(("memory_unavailable", None))
    try:
        usage = shutil.disk_usage(os.fspath(disk_path))
        facts["disk_total"] = int(usage.total)
        facts["disk_free"] = int(usage.free)
    except Exception as error:  # pylint: disable=broad-except
        # `!r`：`OSError` 的 str 會帶出路徑。
        print(f"_resource_report disk_usage failed: {error!r}", file=sys.stderr)
        notes.append(("disk_unavailable", None))
    return facts, notes


def collect(*, disk_path, own_pid: int | None = None, assistant_pids=(),
            cpu_interval: float = DEFAULT_CPU_INTERVAL_SEC,
            scan=None, machine=None, clock=None) -> Snapshot:
    """量一次，回一份 `Snapshot`。**會睡滿 `cpu_interval`，所以要丟執行緒跑。**

    `scan` / `machine` / `clock` 可注入，所以整段判定與合計測得起來而不必依賴這台
    機器真正的行程表——依賴真行程表的測試會在別人的機器上、在批次沒跑的時候、在
    CI 上得到不同的答案，那種測試遲早會被標成 skip。

    * `scan()` → `ScanResult`
    * `machine()` → `(facts_dict, notes)`

    永不 raise：整段包起來，壞掉就回一份「什麼都不知道」的 Snapshot 加一筆 note，
    因為這是診斷指令，它自己炸掉等於在最需要資訊的時候什麼都不說。
    """
    now = (clock or time.time)()
    started = time.monotonic()
    # 只在真的要去問作業系統的時候才補上自己的 pid。注入假行程表的測試不該憑空
    # 多出一個真 pid——那個號碼跟假資料撞上的機率很低，但撞上時的症狀（某一列突然
    # 變成 `bot`）看起來會像判定邏輯壞了，而那是最難查的一種紅燈。
    if own_pid is None and scan is None:
        own_pid = os.getpid()
    notes: list[tuple] = []
    rows: list = []
    scan_ok = True
    system_cpu = None
    descendant_roots: dict = {}
    try:
        if scan is None:
            result = _psutil_rows(
                own_pid=own_pid, cpu_interval=cpu_interval,
                extra_pids=assistant_pids)
        else:
            result = scan()
        rows = list(result.rows)
        scan_ok = result.scan_ok
        system_cpu = result.system_cpu
        descendant_roots = result.descendant_roots
        notes.extend(result.notes or ())
    except Exception as error:  # pylint: disable=broad-except
        print(f"_resource_report collect failed: {error!r}", file=sys.stderr)
        rows, scan_ok, descendant_roots = [], False, {}
        notes.append(("scan_incomplete", None))

    try:
        facts, machine_notes = (machine or (lambda: _machine_facts(disk_path)))()
        notes.extend(machine_notes or ())
    except Exception as error:  # pylint: disable=broad-except
        print(f"_resource_report machine facts failed: {error!r}",
              file=sys.stderr)
        facts = {}
        notes.append(("memory_unavailable", None))
        notes.append(("disk_unavailable", None))

    try:
        roles = summarise(rows, classify(
            rows, assistant_pids=assistant_pids, own_pid=own_pid,
            descendant_roots=descendant_roots))
    except Exception as error:  # pylint: disable=broad-except
        print(f"_resource_report summarise failed: {error!r}", file=sys.stderr)
        roles = {}
        notes.append(("scan_incomplete", None))

    # 同一個鍵只留一筆（`scan_incomplete` 可能從兩條路各來一次），順序保留。
    unique: list[tuple] = []
    seen: set = set()
    for key, detail in notes:
        if key in seen:
            continue
        seen.add(key)
        unique.append((key, detail))

    return Snapshot(
        roles=roles, notes=tuple(unique), scan_ok=bool(scan_ok),
        taken_at=now, elapsed=max(0.0, time.monotonic() - started),
        cpu_interval=cpu_interval,
        cpu_count=facts.get("cpu_count"), cpu_percent=system_cpu,
        mem_total=facts.get("mem_total"),
        mem_available=facts.get("mem_available"),
        disk_total=facts.get("disk_total"), disk_free=facts.get("disk_free"),
        disk_path=facts.get("disk_path") or "")
