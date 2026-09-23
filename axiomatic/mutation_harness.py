"""共用的變異測試骨架：把測試「弄壞一次」的那套流程做對一次，不要每個人重寫。

**為什麼需要它。** 這個 repo 的慣例是每加一批守門就自己把它守的東西弄壞、確認會
紅。慣例是對的，但 2026-09-08 一天之內就有四支各自手寫的骨架，其中**兩支寫錯，而且
錯的方式都會產出「假的 SURVIVED」**——也就是把「測試很弱」的結論栽贓給一組其實
沒問題的測試：

* 一支用 `-k unknown_scan` 選測試，而真正的名字是 `..._unknown_instead_of_zero`。
  **`-k` 選不到任何東西時是 `0 passed`、rc=0**，於是那個變異顯示為 SURVIVED。
* 一支用短錨點做代換，而那段字在檔案裡出現**兩次**，於是被跳過並顯示成 SKIP——
  混在一串 KILLED 中間很容易被讀成「這個變異不適用」。

還有兩條是更早用事故換來的：快照要放 **repo 外**（`finally` 在行程被殺掉時不會跑，
而變異中的檔案留在原地會讓下一次執行從一個壞掉的基準開始），以及**啟動先 heal**。

所以這裡把四條規則落實成程式碼，而不是只寫在註解裡：

1. **用完整 node ID 或整個測試檔選測試，不接受 `-k`。** 而且先跑一次基準，**基準
   必須綠、且收集到的測試數必須大於零**——那是正面對照組，沒有它整批結果都是空轉。

   2026-09-10 放寬：原本連「整個測試檔」也一起擋。擋 `-k` 的理由是「選不到任何
   東西時 pytest 是 `0 passed`、rc=0」，而那個失效模式對**檔案路徑**不成立——路徑
   打錯時 pytest 是用法錯誤，而且現在會先確認檔案存在。規則寫得比它的理由大，代價
   是逼呼叫端**手挑 node ID**，而手挑的測試集正是變異分數最容易灌水的地方：挑到
   剛好守得住的那幾支，分數就滿分。同一天實測過一次——8 支手挑的測試給 9/10，換成
   整份檔案就不是那個數字了。
2. **錨點必須在檔案裡剛好出現一次**，否則直接算失敗（不是靜靜跳過）。
3. **快照放 repo 外，啟動先 heal，每個變異跑完立刻還原**，最後再驗一次位元組相同。
4. **回傳結構化結果**，讓呼叫端可以斷言「全殺」，而不是靠人讀 stdout。

用法（放在 `<repo 外的暫存目錄>\\` 的探針腳本裡）::

    import sys
    sys.path.insert(0, r"D:\\Work\\Example\\axiomatic")
    import _browser_killguard  # noqa: F401  在 pytest 傘外一定要自己掛上
    from mutation_harness import run_mutations

    result = run_mutations(
        target="axiomatic/discord_bot.py",
        tests=["test/test_x.py::test_y"],
        mutants=[("拿掉那道閘", "if guard:", "if False and guard:")],
    )
    print(result.report())
    assert result.all_killed, result.survivors

**這支不做**「自動產生變異」。變異要由人挑，因為有意義的變異是「一個未來的人真的
可能寫出來的改動」，不是隨機翻轉運算子。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import hashlib as _hashlib
import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# ---------------------------------------------------------------------------
# 這個骨架自己把瀏覽器防線裝上，不要靠探針作者記得
# ---------------------------------------------------------------------------
# `_browser_killguard` 的 docstring 誠實記著它的殘餘缺口：「這仍然是『要記得加那一
# 行』」。而 2026-09-07 第三次弄掉正式瀏覽器的，**正是一支變異測試探針**——也就是
# 說，最可能忘記那一行的人，跟最需要那道防線的人，是同一個人。
#
# 所以由這裡代勞：任何探針只要 `from mutation_harness import run_mutations`，防線
# 就在它有機會做任何事之前裝好了。探針自己那一行仍然值得寫（它涵蓋「還沒 import
# 這個骨架就先動手」的情況，而且是唯一能保證**順序**的寫法），但忘了寫不再等於
# 完全沒有保護。
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
import _browser_killguard  # noqa: E402,F401  匯入即生效，不要刪

# 快照的預設位置：**repo 外**。理由見模組 docstring。系統暫存區底下開一個專屬
# 資料夾，`AXIOMATIC_MUTATION_SNAPSHOT_DIR` 可覆寫成任何 repo 外的路徑。
DEFAULT_SNAPSHOT_DIR = Path(
    os.environ.get("AXIOMATIC_MUTATION_SNAPSHOT_DIR")
    or (Path(tempfile.gettempdir()) / "axiomatic_mutation_snapshots"))

# 這個變數只在**本骨架自己開的 child pytest** 的環境裡出現。
#
# 用途是讓 `conftest.py` 分辨「樹是被我故意改壞的」與「樹被上一次沒跑完的變異留在
# 壞掉的狀態」。兩者在磁碟上長得一模一樣（進行中標記還在、實體檔跟快照不同），
# 差別只有「現在是不是真的有一輪在跑」，而那件事只有開 child 的人知道。
CHILD_ENV_FLAG = "AXIOMATIC_MUTATION_CHILD"

KILLED = "KILLED"
SURVIVED = "SURVIVED"
ERROR = "ERROR"
# 「跑不完」與「跑完但紅了」要分開講。兩者都代表變異**沒有存活**，但一個是有測試
# 抓到它、另一個是它把測試拖垮了，下一個讀報告的人需要看得出差別。
TIMEOUT = "TIMEOUT"

# 單一測試的上限。實測基準（2026-09-10，三份完整測試檔）：600 支、合計 107 秒、
# 最慢的一支 3.68 秒，所以 60 秒是 16 倍餘裕。
CHILD_TEST_TIMEOUT_SEC = 60
# 整個 child 的牆鐘上限，後備用：pytest 若在自己的計時器上膛之前就卡住
# （收集期、匯入期、它自己的子行程），上面那道就來不及。
CHILD_RUN_TIMEOUT_SEC = 1800
# child 根本沒跑到測試。**不是擊殺**——沒有證據顯示任何測試對這個變異有反應。
NO_EVIDENCE = "NO-EVIDENCE"
# 變異讓程式碼**根本載不起來**（收集期就爆），所以一支測試都沒執行到。
#
# **這不是擊殺，也不該計分。** pytest 對這種情況回報 `1 error`、rc 非零，跟「斷言
# 抓到變異」在回傳值上長得一樣——於是越笨拙的變異越容易「殺掉」，變異分數被高估。
# 變異測試的文獻把它獨立成一格（Stryker 叫 CompileError／RuntimeError，古典文獻
# 叫 incompetent mutant），而且**從分母裡拿掉**：分數是 `detected / valid`，
# invalid 的那些只列在報告裡。這裡照做。
#
# ⚠️ **但不照做的那一半更重要：本專案讓它把 `all_killed` 拉成 False。** 純粹排除
# 分母是 fail-open 的——一組全部寫壞的變異會得到「0/0，沒有存活者」，而那跟「守門
# 很穩」在輸出上一模一樣（`empty selection looks like a clean result`）。所以它照
# 算在 `survivors` 裡，逼人去看報告。
INCOMPETENT = "INCOMPETENT"
# child 有跑，但**執行到的測試比選到的少**：這一輪在中途停掉了。
#
# 2026-09-20 量到的實例：conftest 把 `ast.walk` 換成攤平版，對它做變異時發現
# **pytest 自己的 traceback 排版也用 `ast.walk`**（`_pytest/_code/source.py` 的
# `get_statement_startend2`）。於是壞掉的 walk 讓第一支測試通過、第二支斷言失敗、
# pytest 在印那個失敗時 INTERNALERROR，整輪當場結束。末行是 `1 passed, 5 warnings`、
# rc 非零——`_executed_any_test` 看得到「有測試跑過」，所以它被判成 KILLED。
#
# **判成 KILLED 不算錯，但那個證據撐不起這個結論**：斷言確實有反應（所以才在印
# 失敗），可是同一組輸出也可能是「執行器自己倒了、沒有任何測試對變異有意見」。
# 手上明明有分母（`collected`，基準那行的「選到 N 支」）卻沒拿來比，正是
# 「a count without its denominator hides a shortfall」那個形狀。少跑就說少跑，
# 讓人去看，不要給一個看起來很篤定的分數。
ABORTED = "ABORTED"

# pytest 有跑到底並回報結果的證據。`-q` 的最後一行永遠是那句摘要
# （`600 passed, 5 warnings in 108.97s`、`1 error in 0.34s`、`no tests ran in
# 0.12s`），所以「最後一行長得像摘要」就是「它真的跑過」。
_REPORTED_OUTCOMES = re.compile(
    r"\b\d+\s+(passed|failed|error|errors|skipped|deselected|xfailed|xpassed)\b"
    r"|\bno tests ran\b")


# 真的**執行**到測試的證據。`1 error in 0.34s`（收集期爆掉）與 `no tests ran`
# 都符合上面那個「有回報摘要」的形狀，卻一支測試都沒跑到——那是兩種不同的事。
_ANY_TEST_EXECUTED = re.compile(r"\b\d+\s+(passed|failed|xpassed|xfailed)\b")


# 摘要裡每一個「N 個某種結果」。`deselected` 刻意不算：那是被選掉、沒有執行。
_OUTCOME_COUNTS = re.compile(
    r"\b(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b")


def _executed_count(tail: str) -> int:
    """摘要裡**總共執行到幾支**。看不出來時回 0。

    `error` 也算執行到：一支測試在 setup 裡爆掉，pytest 印的是 `10 passed, 1 error`，
    而那 11 支都在 `--collect-only` 的數字裡——不算它的話每一次 fixture 出錯都會被
    誤判成「中途停掉」。
    """
    return sum(int(n) for n, _ in _OUTCOME_COUNTS.findall(tail or ""))


def _executed_any_test(tail: str) -> bool:
    """摘要裡有沒有「至少一支測試真的跑了」？"""
    return bool(_ANY_TEST_EXECUTED.search(tail or ""))


def _looks_like_a_test_run(tail: str) -> bool:
    """child 有沒有回報過測試結果？

    這是判定 KILLED 之前的**正面對照組**，和基準那道同一個道理：基準要求「綠、而且
    收集到的測試數大於零」，因為選不到測試的一輪看起來和乾淨的一輪一模一樣。變異那
    一輪缺的正是這個——被 OS 當場殺掉的 child 也是 `rc != 0`，和「斷言抓到變異」在
    回傳值上無法區分，差別只在它什麼都沒說。
    """
    return bool(_REPORTED_OUTCOMES.search(tail or ""))


@dataclass
class MutantResult:
    label: str
    verdict: str
    detail: str = ""
    # 這一筆是從上一次的紀錄沿用的，不是這一輪真的跑出來的。
    #
    # **一定要看得見。** mutmut 的 `.mutmut-cache` 最有名的抱怨（boxed/mutmut#104）
    # 就是「我修好測試再跑一次，它還是報上次的結果」——沿用本身沒錯，錯在它是靜悄悄
    # 的。這個旗標會印進報告裡。
    reused: bool = False


@dataclass
class Result:
    baseline: str
    mutants: list[MutantResult] = field(default_factory=list)
    restored: bool = True

    @property
    def survivors(self) -> list[str]:
        """沒有被抓到的變異。**逾時不算存活**——測試確實對它有反應。"""
        return [m.label for m in self.mutants
                if m.verdict not in (KILLED, TIMEOUT)]

    @property
    def no_evidence(self) -> list[str]:
        """child 沒跑到測試的變異。**這一批的分數不能用**，要重跑。"""
        return [m.label for m in self.mutants if m.verdict == NO_EVIDENCE]

    @property
    def incompetents(self) -> list[str]:
        """讓程式碼載不起來的變異。**不計分**，但照樣算存活（見常數的註解）。"""
        return [m.label for m in self.mutants if m.verdict == INCOMPETENT]

    @property
    def aborted(self) -> list[str]:
        """跑到一半停掉的變異：執行數少於選到的數。同樣**不計分、照算存活**。"""
        return [m.label for m in self.mutants if m.verdict == ABORTED]

    @property
    def timeouts(self) -> list[str]:
        """跑不完的變異。不是存活，但也**不是**一次乾淨的擊殺，要單獨看。"""
        return [m.label for m in self.mutants if m.verdict == TIMEOUT]

    @property
    def all_killed(self) -> bool:
        return bool(self.mutants) and not self.survivors and self.restored

    @property
    def reused(self) -> list[str]:
        """沒有重跑、直接沿用上一次結果的變異。"""
        return [m.label for m in self.mutants if m.reused]

    def report(self) -> str:
        lines = [f"基準：{self.baseline}"]
        for m in self.mutants:
            mark = "（沿用）" if m.reused else ""
            lines.append(f"  {m.verdict:<12}{mark}{m.label}   {m.detail}")
        killed = sum(1 for m in self.mutants if m.verdict == KILLED)
        # 分母是**有效**變異：載不起來的（INCOMPETENT）與根本沒跑到的
        # （NO-EVIDENCE）不計分——它們不是「測試沒抓到」，是「這一格沒有結論」。
        invalid = len(self.no_evidence) + len(self.incompetents) + len(self.aborted)
        valid = len(self.mutants) - invalid
        suffix = f"（另有 {invalid} 個不計分）" if invalid else ""
        lines.append(f"\n{killed}/{valid} 殺掉{suffix}；"
                     f"還原正確：{self.restored}")
        if self.no_evidence:
            lines.append(
                f"■ {len(self.no_evidence)} 個變異的 child **根本沒跑到測試**："
                f"{self.no_evidence}。這幾筆沒有結論，整批分數不可用，重跑。"
                "最常見的原因是這個行程被連帶殺掉（背景工作、session 結束、"
                "job object），child 一開就死、rc 非零、什麼都沒印。")
        if self.incompetents:
            lines.append(
                f"※ {len(self.incompetents)} 個變異讓程式碼**根本載不起來**："
                f"{self.incompetents}。pytest 回的是 `N error`、rc 非零，跟被"
                "斷言抓到長得一樣——但一支測試都沒執行到，所以那不是擊殺。已從"
                "分母拿掉（越笨拙的變異越容易「殺掉」，那會高估分數），但仍算"
                "存活，因為「全部寫壞」與「守門很穩」在輸出上不該長得一樣。"
                "把變異改成語法合法、只改語意的形狀再跑一次。")
        if self.aborted:
            lines.append(
                f"※ {len(self.aborted)} 個變異**跑到一半停掉**（執行數少於選到的"
                f"數）：{self.aborted}。末行看得到「有測試跑過」、rc 也非零，但那一輪"
                "沒跑完——斷言有沒有抓到它，這組證據分不出來。最常見的原因是變異改到"
                "了 pytest 自己也在用的東西（排版、收集、斷言改寫），於是它在印第一個"
                "失敗時就倒了。看一眼 child 的輸出，或把變異改小一點再跑一次。")
        if self.timeouts:
            lines.append(
                f"※ {len(self.timeouts)} 個變異是**跑不完**（不是被斷言抓到）："
                f"{self.timeouts}。把測試拖垮也算擋下來了，但那通常代表那個變異"
                "打開了一條指數級的路徑，值得單獨看一眼。")
        return "\n".join(lines)


def _child_env() -> dict[str, str]:
    """child pytest 的環境：照抄本行程的，另外插上「這是變異骨架開的」旗標。

    一定要**整份帶過去**（`{**os.environ, ...}`）而不是只給那一個變數——child 要
    `PATH`、`SYSTEMROOT`、`TEMP` 才起得來，Windows 上少了 `SYSTEMROOT` 連 socket
    都初始化不了。
    """
    return {**os.environ, CHILD_ENV_FLAG: "1",
            "PYTHONIOENCODING": "utf-8"}


def _per_test_timeout_args() -> list[str]:
    """pytest-timeout 有裝就用它。

    這是兩道時間上限裡**比較好的那一道**：它把「掛住」變成「一支有名字的紅測試」，
    而不是一段沉默的牆鐘。沒裝就退回只有下面那道後備，不強迫多一個相依。
    `--timeout-method=thread`：Windows 沒有 `SIGALRM`，signal 那種在這台機器上
    根本不會生效。
    """
    if importlib.util.find_spec("pytest_timeout") is None:
        return []
    return [f"--timeout={CHILD_TEST_TIMEOUT_SEC}", "--timeout-method=thread"]


def _pytest(tests: list[str], *, extra: list[str] | None = None):
    """跑一次 child pytest。回 `(rc, 最後一行)`；**逾時的 rc 是 `None`**。

    用 `None` 而不是某個假的非零 rc，是為了讓呼叫端**沒辦法**把逾時誤讀成一般的
    失敗——那兩件事在報告裡必須長得不一樣。
    """
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-q", "--no-header",
             *_per_test_timeout_args(), *(extra or [])],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_child_env(),
            timeout=CHILD_RUN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None, (f"child 超過 {CHILD_RUN_TIMEOUT_SEC}s 還沒跑完，已強制結束")
    tail = (done.stdout or "").strip().splitlines()
    if tail:
        return done.returncode, tail[-1]
    # stdout 空的時候 stderr 通常正好寫著原因（`ImportError while loading
    # conftest`、`ERROR: file or directory not found`）。舊版把它丟掉，於是
    # 2026-09-10 那次假的滿分只剩下牆鐘算術可以推。
    err = (done.stderr or "").strip().splitlines()
    if err:
        return done.returncode, f"（stdout 是空的）stderr 末行：{err[-1]}"
    return done.returncode, "(沒有輸出)"


def _collected_count(tests: list[str]) -> int:
    """先問「這組 node ID 到底選到幾支」。零就是呼叫端打錯了名字。"""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "--collect-only", "-q",
         "--no-header"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=_child_env(),
        timeout=CHILD_RUN_TIMEOUT_SEC)
    return sum(1 for line in (done.stdout or "").splitlines() if "::" in line)


def _project_fingerprint() -> str:
    """整個專案原始碼的指紋——沿用上一次結果的唯一依據。

    **為什麼要涵蓋整個專案，而不只是被變異的那個檔。** 一個變異會不會被殺掉，取決
    於「這一組測試對它有沒有反應」，而測試住在別的檔案裡；被測模組的相依也住在別的
    檔案裡。只對目標檔取指紋的話，「我改了測試想看它現在抓不抓得到」正好是指紋看不
    見的那種改動——於是沿用一筆過期的結論，而且沒有任何徵兆。

    這正是 mutmut 的 `.mutmut-cache` 最常被抱怨的地方（boxed/mutmut#104：修好測試
    再跑一次，報的還是上次的結果）。快取本身是對的——mutmut 與 Cosmic Ray 都做，
    因為一輪動輒幾十分鐘——錯的是讓「沿用」變成一件看不見的事。所以這裡的判準嚴到
    近乎粗暴：**專案的每一個 `.py` 都要位元組相同**，否則整份紀錄作廢。

    代價是「改一行不相干的程式碼就要重跑」，而那個代價是刻意付的：這支工具的失效
    方向只有一個，就是產出一個看起來很正常的錯誤結論。

    範圍是套件 ＋ `test/` ＋ repo root。測試 2026-09-22 從套件搬到 repo 根目錄的
    `test/`；少了中間那一項，「我改了測試再跑一次」就又變回指紋看不見的改動。
    `test/` 從 `PROJECT_ROOT` 當場推出來，換掉 `PROJECT_ROOT` 的測試會跟著換。
    """
    digest = _hashlib.sha256()
    for path in (sorted(PACKAGE_ROOT.glob("*.py"))
                 + sorted((PROJECT_ROOT / "test").glob("*.py"))
                 + sorted(PROJECT_ROOT.glob("*.py"))):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _journal_path(snapshot_dir: Path, live: Path) -> Path:
    return snapshot_dir / (live.name + ".journal")


def _load_journal(path: Path, fingerprint: str, tests: list[str]) -> dict:
    """讀上一次的結果，指紋或測試組不合就當作沒有。

    回傳 `{(label, old, new): MutantResult}`。任何讀取失敗都回空的——這是純粹的
    最佳化，**壞掉的紀錄要退回「重跑」，不是往上拋**。
    """
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("fingerprint") != fingerprint or data.get("tests") != tests:
        return {}
    out = {}
    for row in data.get("results", []):
        try:
            key = (row["label"], row["old"], row["new"])
            verdict = row["verdict"]
        except (KeyError, TypeError):
            continue
        # **沒有結論的那幾格不沿用。** `NO-EVIDENCE` 的意思就是「這一格沒有結論，
        # 重跑」——把它存起來下次直接端出來，等於把「重跑」這個指示本身快取掉。
        # `TIMEOUT` 同理：它多半是主機當下的負載，不是這個變異的性質。
        # 兩者剛好也是最可能因為「上一次被砍掉」而產生的兩種值。
        if verdict in (NO_EVIDENCE, TIMEOUT):
            continue
        out[key] = MutantResult(row["label"], verdict,
                                row.get("detail", ""), reused=True)
    return out


def _save_journal(path: Path, fingerprint: str, tests: list[str],
                  rows: list[tuple]) -> None:
    """把這一輪的結果寫下來，給下一次沿用。

    寫在**每個變異跑完之後**，不是全部跑完之後——這份紀錄存在的理由就是「行程可能
    活不到最後」，收工才寫等於沒寫。

    用同目錄暫存檔 + `os.replace`（跨行程檔案的硬規則）：`conftest` 不讀這一份，
    但下一次的 `_load_journal` 會讀，而那可能發生在這一份寫到一半被砍掉之後。
    """
    payload = {"fingerprint": fingerprint, "tests": tests,
               "results": [{"label": label, "old": old, "new": new,
                            "verdict": mutant.verdict, "detail": mutant.detail}
                           for label, old, new, mutant in rows]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _marker_fields(raw: str) -> tuple[str, int | None]:
    """進行中標記的內容 → `(目標檔, 寫下這個標記的行程 pid)`。

    格式**刻意向後相容**：第一行是目標檔，第二行（有的話）是 pid。2026-09-12 之前
    寫的標記只有一行，照樣讀得出目標檔，pid 回 `None`。
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    target = lines[0] if lines else ""
    pid = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None
    return target, pid


def live_mutation_runs(snapshot_dir: Path | None = None) -> list[tuple[str, int]]:
    """**現在真的有一輪在跑**的那些 `(目標檔, pid)`。

    `interrupted_targets()` 回答的是「樹現在是不是髒的」，而它**答不了**「髒是因為
    上一輪被砍掉，還是因為有一輪正跑到一半」——兩者在磁碟上一模一樣。這個區別決定
    了下一步完全相反：被中斷要**還原**，正在跑要**不要碰**。

    2026-09-12 付過代價：一個 subagent 把變異跑在背景、自己的回合先結束，主 agent
    照 `conftest.py` 訊息裡的補救方式把 `.orig` 複製回去並**刪掉標記**——而那一輪
    還活著。結果是 (1) 還原落在兩個變異之間，下一輪把新的變異寫回去；(2) 標記沒了
    之後 `interrupted_targets()` 回 `[]`，偵測器瞎掉而樹是髒的。
    **標記是唯一在說「有一輪在飛」的東西，所以永遠不要為了整理而刪它。**

    判準用 `_chrome_slot._pid_alive`（不另外再寫一份）：它在判不出來時回 **True**，
    而這裡要的正是那個方向——分不出來時寧可當作有人在跑、不要動樹。
    **舊的單行標記沒有 pid，不會出現在這裡**：那是過渡產物，維持 2026-09-12 之前的
    行為（照舊視為「上一輪沒跑完」），不因此把每一個舊標記都變成永遠不能 heal。
    """
    # ⚠️ **匯入放在函式裡，而且失敗就安靜退場。** 這支的呼叫端之一是
    # `conftest.pytest_configure`——那裡任何一個未捕捉的例外都會讓**整套測試收集
    # 不起來**，而 traceback 會長得像磁碟壞掉（本專案記過這條：一個讀取的波及範圍
    # 由它的呼叫端決定）。判不出來時退回 2026-09-12 之前的行為就好：
    # `interrupted_targets()` 照樣會擋下收集，只是少了那句「有人正在跑」的提示。
    try:
        from _chrome_slot import _pid_alive  # noqa: PLC0415  只有這一支用得到
    except Exception:  # pylint: disable=broad-except
        return []

    snapshot_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    if not snapshot_dir.is_dir():
        return []
    out: list[tuple[str, int]] = []
    for marker in sorted(snapshot_dir.glob("*.inprogress")):
        try:
            target, pid = _marker_fields(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # 正常收工的競態
        except (OSError, UnicodeDecodeError):
            continue  # 讀不出來 → 交給 `interrupted_targets()` 算髒
        if target and pid and _pid_alive(pid):
            out.append((target, pid))
    return out


def interrupted_targets(snapshot_dir: Path | None = None) -> list[str]:
    """哪些檔案**現在正躺在變異狀態**：進行中標記還在，而且實體檔跟快照不同。

    回傳的是 repo 相對路徑（就是標記檔裡寫的那個字串），空清單＝乾淨。

    **為什麼需要這個查詢。** `run_mutations` 的 heal 是在**它自己**下一次啟動時才
    跑的，可是被殺掉之後最可能發生的下一件事，是有人（或某個自動流程）跑一次完整
    測試——那時樹還是壞的，於是得到一批**假紅**，而且斷言會指向業務邏輯，看起來
    完全像是自己剛改壞的。2026-09-10 實際發生：一輪三個變異的驗證跑到第二個時
    行程被砍，`test_bot_helpers.py` 少了 226 個位元組（別名不動點那一段被換成
    `pass`）留在原地，標記也還在。

    這支只**回報**、不修理。修理的權力留給 `run_mutations`，理由跟 heal 本身一樣：
    「檔案跟快照不一樣」也可能是有人正常地編輯了它，自動還原會把合法的修改靜靜地
    退掉（模組 docstring 與 heal 那段註解記著那次事故）。標記存在只是把「上一次
    沒跑完」這件事釘住，不足以推翻「這是人寫的」。

    兩個條件都要成立才算數：

    * **標記在**——只比對檔案與快照是不夠的，那分不出「被殺掉留下的變異」與
      「有人正常編輯過」。
    * **內容真的不同**——只看標記也不夠。乾淨收工時標記會被刪掉，但還原失敗那條
      路徑會**刻意把標記留著**（見 `run_mutations` 結尾），而且被殺掉的時機也可能
      正好在「還原完、還沒刪標記」之間。那些情況下樹是好的，不該擋任何人。

    第三種情況是**兩個條件都問不出來**：標記本身讀不出來（內容不是 UTF-8、或
    I/O 失敗）。那一律算髒，理由寫在程式碼裡——重點是「查不出來」不可以當成
    「乾淨」。唯一的例外是標記在讀之前就被刪掉了，那是正常收工的競態。
    """
    snapshot_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    if not snapshot_dir.is_dir():
        return []
    dirty: list[str] = []
    for marker in sorted(snapshot_dir.glob("*.inprogress")):
        try:
            target, _pid = _marker_fields(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # glob 與這一行之間標記被刪掉了——那是**正常收工**的競態，不是髒。
            continue
        except (OSError, UnicodeDecodeError):
            # 讀不出內容 → **算髒**。這條刻意跟下面「快照或實體檔不在」那條相反，
            # 因為兩者的證據強度不同：那一條有良性解釋（本來就沒動過那個檔），
            # 這一條沒有——標記是這支工具自己用 UTF-8 寫的（見 `run_mutations`
            # 的 `marker.write_text(...)`），讀不回來代表有
            # 東西把它弄壞了，而最可能弄壞它的，就是那個「順便把變異留在樹上」的
            # 強制中斷。
            #
            # 更關鍵的是：讀不出目標檔名就**無法**評估第二個條件（內容是否不同）。
            # 「查不出來」不可以當成「乾淨」——那正是這支查詢要堵的洞。
            dirty.append(f"{marker.name}（標記內容讀不出來，判不出是哪一個檔）")
            continue
        if not target:
            continue
        live = PROJECT_ROOT / target
        snap = snapshot_dir / (live.name + ".orig")
        # 快照或實體檔不在就沒得比。**不當成髒的**——沒有證據就不要擋人，
        # 這道查詢的價值在於它平常完全安靜。
        if not (live.is_file() and snap.is_file()):
            continue
        if live.read_bytes() != snap.read_bytes():
            dirty.append(target)
    return dirty



# --- 正在跑的正式行程會不會重新載入這個檔案 --------------------------------

# 產圖批次的兩個進入點。變異骨架寫檔的那幾十秒裡，只要監督者把子行程重生
# （OOM、定期重啟、當掉），新行程就是**從磁碟重新讀** `.py`——於是變異版會真的
# 跑起來，對著真的瀏覽器、真的帳號。
_BATCH_ENTRY_POINTS = ("webrunner_novelai", "webrunner_je_only")


def _import_closure(entry: str) -> set[str]:
    """`entry` 這個模組在本套件內的匯入閉包（含自己）。

    只看靜態 import，而且只認本套件裡真的存在的模組——夠了：這裡要回答的是
    「重生的子行程會不會載入這個檔案」，而那條路就是一般的 import。
    """
    package = PROJECT_ROOT / "axiomatic"
    known = {p.stem for p in package.glob("*.py")}
    seen: set[str] = set()
    stack = [entry]
    while stack:
        name = stack.pop()
        if name in seen or name not in known:
            continue
        seen.add(name)
        path = package / f"{name}.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        # 帶點的名字**每一段都推**，不只第一段：`from axiomatic._process_control
        # import x` 的第一段是套件名，真正的模組在第二段。只取 `[0]` 的版本就是這樣
        # 讓 `_supervisor` 裡那行延後匯入整個隱形（2026-09-20，`_process_control`
        # 明明在批次閉包裡，對它跑變異卻沒被擋）。多推的段落不在 `known` 裡，
        # 迴圈開頭就濾掉了，所以不會把 stdlib 或套件名本身混進來。
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    stack.extend(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    stack.extend(node.module.split("."))
                for alias in node.names:
                    stack.append(alias.name)
    return seen


def batch_modules_at_risk() -> set[str]:
    """正在跑的產圖批次重生時會載入的模組（沒有批次在跑就回空集合）。"""
    if not _batch_is_running():
        return set()
    at_risk: set[str] = set()
    for entry in _BATCH_ENTRY_POINTS:
        at_risk |= _import_closure(entry)
    return at_risk


def _batch_is_running() -> bool:
    """磁碟上的 pid 檔指向一個還活著的行程嗎？

    判準刻意用 pid 檔而不是掃描命令列：pid 檔是這個專案自己的契約（監督者寫、
    大家讀），掃描則要處理轉接殼配對那一堆事，而那些已經有別人在做了。
    """
    pid_file = PROJECT_ROOT / "webrunner.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        from _chrome_slot import _pid_alive  # noqa: PLC0415  只有這一支用得到
    except Exception:  # pylint: disable=broad-except
        # 判不出來就當作「有在跑」——這道防線的預設要往保守那邊倒。
        return True
    return bool(_pid_alive(pid))


class LiveBatchConflict(Exception):
    """要變異的檔案，正在跑的產圖批次重生時會載入它。"""


def _refuse_if_a_live_batch_would_reload(target: str) -> None:
    """正式批次在跑時，不准變異它會重新載入的模組。

    **為什麼是拒絕而不是警告。** 變異骨架把變異版**寫進真的檔案**，跑完才還原，
    一輪下來磁碟上帶著變異版的時間是數十秒到數分鐘。同一時間，產圖批次由監督者
    看著，而它會因為 OOM、定期重啟、瀏覽器死掉而重生——重生就是一個**全新的
    Python 行程從磁碟讀 `.py`**。兩件事撞在一起，變異版就會對著真的瀏覽器、真的
    帳號跑起來。

    而這條路上最貴的那幾個變異正好是最想測的那些：關閉額度對話框的邏輯，它的
    契約是「絕對不能按到會花錢（甚至會退訂）的按鈕」。把那段邏輯反過來寫、放到
    磁碟上三十秒，就是在賭監督者這三十秒內不要重生。

    這個專案已經為「測試工具弄壞正式環境」付過三次代價（2026-09-07 一天之內，
    `_browser_killguard.py` 就是那次的產物）。那三次是**殺掉**正式瀏覽器；這一條
    防的是更安靜的版本——**讓正式環境跑我們寫壞的程式碼**。

    範圍刻意只有產圖批次的匯入閉包（9 個模組），不含 bot：bot 的閉包大得多、改它
    是日常工作，而它沒有「一個誤點就花錢」的那條路。真的要跑就等批次結束，或傳
    `allow_live_batch=True` 明講你知道自己在做什麼。
    """
    at_risk = batch_modules_at_risk()
    if not at_risk:
        return
    stem = Path(target).stem
    if stem not in at_risk:
        return
    raise LiveBatchConflict(
        f"`{target}` 正在被一個**跑著的**產圖批次使用（或它重生時會載入）。\n"
        "變異骨架會把變異版寫進真的檔案數十秒；這段期間監督者只要重生子行程"
        "（OOM／定期重啟／瀏覽器死掉），那個全新行程就會**從磁碟讀進變異版**，"
        "對著真的瀏覽器與真的帳號跑起來。這條路上最貴的變異正好是最想測的那些"
        "（關閉額度對話框的邏輯，契約是絕不按到會花錢的按鈕）。\n"
        "等這一輪批次結束再跑，或明確傳 `allow_live_batch=True`。")


def run_mutations(*, target: str, tests: list[str],
                  mutants: list[tuple[str, str, str]],
                  snapshot_dir: Path | None = None,
                  progress: "Callable[[MutantResult], None] | None" = None,
                  resume: bool = False,
                  allow_live_batch: bool = False) -> Result:
    """對 `target` 逐一套用 `mutants`，每次跑 `tests`，回結構化結果。

    `mutants` 的每一筆是 `(標籤, 原文, 替換文)`；原文必須在檔案裡**剛好出現一次**。

    `resume=True` 會沿用上一次同一組（同指紋、同測試組、同變異文字）的結果，跳過
    那幾輪。預設 **False**：沿用是最容易產生「看起來很正常的錯誤結論」的地方，要
    自己開。開了之後沿用的那幾筆在報告裡會標「（沿用）」——mutmut 與 Cosmic Ray
    都做這件事（一輪動輒幾十分鐘），出問題的從來不是快取本身，是看不見的快取。

    `progress` 每跑完一個變異就收到那一筆 `MutantResult`。預設 None＝安靜。
    **會用到它的理由是行程可能活不到最後**：每個變異要一次完整的測試回合（本機
    ~100 秒），一輪十個就是十幾分鐘，而 `result.report()` 是全部跑完才印的——
    2026-09-10 一輪跑到第二個變異時 host 行程結束，於是第一個變異的結果（已經花掉
    的 100 秒）跟著消失，什麼都不剩。傳一個會 flush 的印函式進來，被砍掉時至少
    留得住已經付過代價的那幾筆。
    """
    if not allow_live_batch:
        _refuse_if_a_live_batch_would_reload(target)
    for name in tests:
        # 測試 2026-09-22 起住在 repo 根目錄的 `test/`，node ID 一律以它開頭。
        if not name.startswith("test/"):
            raise ValueError(
                f"`tests` 只接受完整 node ID 或整個測試檔（`test/` 開頭），拿到 {name!r}。"
                "不要用 `-k`：選不到任何測試時 pytest 是 `0 passed`、rc=0，"
                "整批變異會顯示成 SURVIVED，而那是假的。")
        if "::" in name:
            continue
        # 整個檔案是允許的，但**必須真的存在**。擋 `-k` 的那個理由（選不到東西
        # 卻是 rc=0）對檔案路徑不成立：路徑打錯時 pytest 是用法錯誤、不是綠燈，
        # 而這裡再確認一次，那條路就完全關上了。
        if not (PROJECT_ROOT / name).is_file():
            raise ValueError(
                f"`tests` 給了一個不存在的測試檔：{name!r}（完整 node ID 或"
                "整個測試檔都可以，但檔案要真的在）。打錯路徑而沒有人擋的話，"
                "整批變異會變成沒有測試在跑。")

    snapshot_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    live = PROJECT_ROOT / target
    snap = snapshot_dir / (live.name + ".orig")

    # heal：上一次可能被砍掉、留下一個變異中的檔案。
    #
    # ⚠️ **只有在「上一次沒有跑完」時才可以還原。** 第一版是「檔案跟快照不一樣就
    # 還原」，那個判斷是錯的——它分不出「上一次被砍掉留下的變異」與「這段期間有人
    # 正常地編輯了這個檔案」，於是**把合法的修改靜靜地退掉**。2026-09-08 寫這支的
    # 當天就被它咬到：我改完 `mutation_harness.py` 之後拿它自己跑自我驗證，heal 拿
    # 三分鐘前的快照把我剛寫的修正整段還原掉，而且什麼都沒說——症狀只是「基準莫名
    # 其妙變紅」。這正是這支工具要消滅的那一類（安靜的錯誤結果）。
    #
    # 所以改成用「進行中」標記：跑之前建立、乾淨收工時刪掉。**標記還在 = 上一次
    # 沒跑完**，那才是唯一該還原的情況。
    marker = snapshot_dir / (live.name + ".inprogress")
    if marker.exists() and snap.exists() \
            and live.read_bytes() != snap.read_bytes():
        shutil.copy2(snap, live)
    marker.unlink(missing_ok=True)
    shutil.copy2(live, snap)

    collected = _collected_count(tests)
    if collected == 0:
        raise AssertionError(
            f"這組 node ID 一支測試都沒選到：{tests}。"
            "（打錯名字會是 `0 passed`、rc=0——跟『測試通過』長得一模一樣。）")

    rc, baseline = _pytest(tests)
    if rc != 0:
        raise AssertionError(f"基準就是紅的，先修好再做變異：{baseline}")

    # 目標檔的原文只讀一次。**注意它讀的是快照不是實體檔**——每一輪都從同一份原文
    # 出發這件事，本來就是「讀 `snap` 而不是讀 `live`」保證的，不是「放在迴圈外」
    # 保證的。移出迴圈純粹是少做 N 次 I/O，以及讓下面那個解碼失敗只需要處理一次。
    try:
        original = snap.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        # **刻意不用 `errors="replace"`。** 替代字元會讓錨點比對錯位，於是變異看似
        # 套用成功、實際上改到別的地方——又一個「假的 KILLED／SURVIVED」來源。
        # 這支工具只處理本專案自己的原始碼，那些一律是 UTF-8（`CLAUDE.md` 的跨領域
        # 硬規則），所以解不開代表指錯了檔案，該當場講清楚。
        raise AssertionError(
            f"{target} 不是 UTF-8（第 {error.start} 個位元組解不開），"
            "沒辦法做文字代換。變異測試只適用於本專案自己的原始碼。") from error

    result = Result(baseline=f"{baseline}（選到 {collected} 支）")

    # 指紋在**任何變異套用之前**算，所以它描述的是這棵樹乾淨時的樣子。
    fingerprint = _project_fingerprint()
    journal = _journal_path(snapshot_dir, live)
    previous = _load_journal(journal, fingerprint, tests) if resume else {}
    done: list[tuple] = []

    def record(mutant: MutantResult, key: tuple) -> None:
        """收一筆結果，順便讓呼叫端即時知道、並把紀錄寫到磁碟上。

        `progress` 是呼叫端給的，所以它可能會爆。**爆了不可以連累這一輪**——
        報告是次要的，把樹還原回去才是主要的，而 `finally` 只保護得了迴圈本體。
        寫紀錄同理：那是給下一次用的最佳化，失敗不值得中斷這一輪。
        """
        result.mutants.append(mutant)
        done.append((*key, mutant))
        try:
            _save_journal(journal, fingerprint, tests, done)
        except OSError:
            pass
        if progress is not None:
            try:
                progress(mutant)
            except Exception:  # noqa: BLE001  進度回報不值得中斷還原
                pass

    # 從這裡開始，檔案隨時可能是變異狀態。標記在乾淨收工時才刪。
    # **第二行是 pid**，這樣「有人正在跑」與「上一輪沒跑完」才分得開
    # （見 `live_mutation_runs` 的 docstring——2026-09-12 為此付過代價）。
    marker.write_text(f"{target}\n{os.getpid()}\n", encoding="utf-8")
    try:
        for label, old, new in mutants:
            key = (label, old, new)
            if key in previous:
                record(previous[key], key)
                continue
            source = original
            hits = source.count(old)
            if hits != 1:
                record(MutantResult(
                    label, ERROR,
                    f"錨點出現 {hits} 次（必須剛好 1 次；"
                    "多筆就把前後文帶進來，零筆就是原文已經變了）"), key)
                continue
            live.write_text(source.replace(old, new, 1), encoding="utf-8")
            try:
                rc, tail = _pytest(tests)
            finally:
                shutil.copy2(snap, live)
            if rc is None:
                verdict = TIMEOUT
            elif not _looks_like_a_test_run(tail):
                # rc 非零但沒有任何測試結果 → 不知道，**不是**擊殺。
                verdict = NO_EVIDENCE
                tail = (f"rc={rc!r}，但 child 沒有回報任何測試結果。"
                        f"末行：{tail}")
            elif not _executed_any_test(tail):
                # 有摘要、但一支測試都沒執行到 → 變異讓程式碼載不起來（或把測試
                # 全部選掉了）。**不是擊殺**，也不計分。
                verdict = INCOMPETENT
                tail = (f"rc={rc!r}，一支測試都沒執行到——這個變異讓程式碼載不"
                        f"起來，不是被斷言抓到。末行：{tail}")
            elif _executed_count(tail) < collected:
                # 有跑，但沒跑完。**分母就在手上**（基準那行的「選到 N 支」），
                # 不比就等於把「執行器自己倒了」讀成一次乾淨的擊殺。
                verdict = ABORTED
                tail = (f"rc={rc!r}，只執行到 {_executed_count(tail)}/{collected} 支"
                        f"就停了。末行：{tail}")
            elif rc != 0:
                verdict = KILLED
            else:
                verdict = SURVIVED
            record(MutantResult(label, verdict, tail), key)
    finally:
        shutil.copy2(snap, live)

    result.restored = live.read_bytes() == snap.read_bytes()
    if result.restored:
        # 只有確認還原成功才拿掉標記。還原失敗就把標記留著，下一次啟動會 heal。
        marker.unlink(missing_ok=True)
    return result
