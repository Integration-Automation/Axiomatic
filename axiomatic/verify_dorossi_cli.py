#!/usr/bin/env python3
"""verify_dorossi_cli.py — 手動驗證：後端 CLI 的串流契約還跟 bot 對得上嗎。

    py -3 axiomatic/verify_dorossi_cli.py
    py -3 axiomatic/verify_dorossi_cli.py --exe <另一份 claude 執行檔>

**什麼時候跑。** CLI 自動更新之後，以及看門狗或帳本行為怪怪的時候（一輪莫名被砍、答完了卻回「暫時無法回應」、
每一輪後面都跟一個壓縮輪、花費觸發每輪都過門檻）。它真的會打後端、用小模型，每次約
美金幾分錢——所以跟 `verify_external_apis.py` 一樣是**手動入口，不是 pytest 測試**。

**為什麼需要它。** bot 依賴的是 CLI 串流的**形狀**，而那個形狀是別人的程式在決定：
2026-09-19 一天之內就量到三件事都是「上游改了、我們這裡安靜地錯」——成功的答案被判成
用量上限（§8.70）、resume 的金額變成工作階段累計（§8.71）、純聊天的列舉黑名單漏了
18～22 個工具（§8.72）。單元測試只能餵**我們以為**的事件形狀；只有真的打一次才知道
上游現在送的是什麼。

**硬性條件——改這支之前先讀：**

* **用 bot 自己的程式碼，不准自己寫一份解析器。** 每一行輸出都交給
  `dorossi_backend._ClaudeStreamState.feed`，判定交給 `_claude_stream_verdict`，計帳交給
  `_dorossi_round_info_and_record`（它內部再走 `_dorossi_cc_round_info` →
  `_dorossi_cc_account_round`），脈絡大小交給 `_dorossi_context_tokens`。自己再解析一次
  的話，驗到的是那份副本——上游改了形狀，副本跟著「對」，而 bot 照樣壞。旁邊的
  `_Call._observe` 只記 bot **不讀**的欄位（init 的工具列表、`apiKeySource`、
  `memory_paths`），那些正是用來當警報的東西。
* **argv 也是 bot 那一份。** 由 `dorossi_backend._dorossi_cc_argv` 組——
  `_dorossi_via_claude_code` 用的就是它（`test_verify_dorossi_cli` 兩邊都釘住）。這裡只
  換模型（小模型）與在第 3、4 次呼叫**後面**多接工具白名單。
* **不碰正式狀態。** 帳本（`DOROSSI_USAGE_FILE`）與工作階段檔（`DOROSSI_SESSION_FILE`）
  先導到暫存目錄才打第一個呼叫；`_Call.finish` 另外**拒絕**寫進 repo 底下的任何帳本
  （結構性的判準，不是只比對一個檔名）。
* **工作目錄在 repo 外面**（系統暫存目錄底下固定的一個資料夾）。放在 repo 裡的話 CLI 會
  把 repo 的指示檔自動載進去，量到的就不是乾淨的形狀；用固定名稱而不是每次新建，是因為
  CLI 以工作目錄為鍵保存工作階段，每次新建會讓那個目錄越堆越多。
* **環境照 bot 的來。** 以 `_dorossi_cc_child_env` 為底（含背景工作等待上限那個環境
  變數；bot 的 builder 也已經拿掉會讓 CLI 改走 API key 計費或進 bare 模式的變數，
  見 `dorossi_backend._DOROSSI_CC_DROPPED_ENV`），再拿掉名稱以 `CLAUDE` 開頭的變數
  與 `AI_AGENT`：2026-09-19 用 psutil 量過，
  正在跑的 bot 行程**一個都沒有**，而一個開在互動式 CLI 工作階段裡的殼帶著 11 個——
  其中一個會改掉子行程的思考力度。`--exe` 指定的執行檔另外帶
  `DISABLE_AUTOUPDATER=1`：那是一份刻意釘住版本的受測對象，讓它自己更新就失去意義。
  子行程不是 Python，所以 `PYTHONIOENCODING` 那條規則管不到它；它的 stdout 是 UTF-8
  的 JSON，這裡照 bot 的做法以位元組讀、`utf-8`／`replace` 解。

**四項檢查**（各印一行，最後一行是結論）：

1. **純聊天旗標**：init 的工具列表必須是**空的**（CLI 哪天又把工具放進純聊天，這一行
   就會紅）；答案由 `result.result` 取得且非空；`stream_event` 的 `text_delta` 累積出的
   預覽文字與答案一致；工作階段 id、`rate_limit_event`（重設時刻＋已知詞彙裡的狀態）、
   CLI 版本都讀得到。
2. **逐次計帳**：同一個工作階段再 resume 兩次，再做一次用 Glob 工具的回合。每次都走 bot
   的換算，基準照 bot 的做法一輪一輪往下傳。換算後的 token 不得超過這次叫用頂層 `usage`
   （兩版 CLI 都是每次叫用）太多、也不得逐輪長大；金額要是正的；標籤要跟版本對得上
   （每次叫用＝`call`、累計＝`delta`）。工具回合另外驗：`usage.iterations` 最後一筆讀得
   到，而且 bot 的脈絡大小（壓縮觸發用的那個數）遠小於整次叫用的加總。
3. **背景工作訊號**：完整工具形狀（只開 Bash）的新工作階段，要模型在背景跑一個
   `sleep 20` 然後結束這一回合。`_ClaudeStreamState.background_tasks` 必須在串流途中
   非空過（看門狗的閒置／沉默抑制就靠它），串流要自己結束、以成功的 result 收尾。
4. **不是 bare 模式**：官方 headless 文件寫著 `--bare`「將來會成為 `-p` 的預設」。bare
   只讀 API key、不讀登入，也不自動載入指示檔。那一天不該變成一串看不出原因的泛用失敗，
   所以從第 1 次呼叫的 init 與 result 判斷，像 bare 就印一行明講。判準是**量出來**的
   （CLI 2.1.276，本機沒有設定 API key）：

   | | 一般模式 | `--bare` |
   |---|---|---|
   | init 的 `memory_paths` | `{"auto": …}` | **整個鍵不存在** |
   | init 的 `apiKeySource` | `"none"` | `"none"`（分不出來） |
   | `rate_limit_event` | 有 | 沒有 |
   | result | 成功、答案 | rc=1、`is_error`、`subtype` 竟然還是 `success`、`terminal_reason` 是 `api_error`、文字是「Not logged in · Please run /login」 |

   所以判準是三條任一：缺 `memory_paths`、`apiKeySource` 不是 `none`（驗證改走 API key
   ＝計費不再走登入方案，bot 的設計前提就沒了）、result 是登入類的驗證錯誤。

**結論行**（呼叫端 grep 這一行；比照 `verify_browser.py`）：

    VERIFY-DOROSSI-CLI: OK (4 checks)                  exit 0
    VERIFY-DOROSSI-CLI: FAIL (4 checks) failed: 1,4    exit 1
    VERIFY-DOROSSI-CLI: SKIP (0 checks) <原因>         exit 3

SKIP ＝「什麼都沒驗」（PATH 上沒有 `claude`），仍然是非零結束碼——沒驗到不能回報成功。
用 3 不用 2，因為 argparse 參數打錯時自己就 exit 2，而那條路一行結論都不會印。

整支有上限：每一次呼叫最多 `CALL_TIMEOUT_SEC` 秒，每一步之前先印一行進度（呼叫端可能是
有「輸出沉默就砍」backstop 的自走迴圈）。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dorossi_backend as db  # noqa: E402

RESULT_PREFIX = "VERIFY-DOROSSI-CLI:"
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 3
CHECK_COUNT = 4

# 小模型：驗的是串流形狀，不是答案品質。走 bot 的 allowlist 表取值，不另寫字面值。
MODEL_KEY = "haiku"
# 每一次呼叫的牆鐘上限（秒）。同一個值也交給 CLI 當它自己的背景工作等待上限
# （`_dorossi_cc_child_env`），跟 bot 一樣兩者用同一個數。
CALL_TIMEOUT_SEC = 180.0
# 一行 NDJSON 可以很大（result 事件），比照 bot。
_STREAM_LIMIT = 16 * 1024 * 1024
_SCRATCH_NAME = "axiomatic_verify_dorossi_cli"
_GLOB_FILES = ("f0.txt", "f1.txt", "f2.txt")

PROMPT_PING = "Reply with exactly the word PONG and nothing else."
PROMPT_TWO = "Reply with exactly the word TWO and nothing else."
PROMPT_THREE = "Reply with exactly the word THREE and nothing else."
PROMPT_GLOB = ("Use the Glob tool three separate times, one call per message: first "
               "with the pattern f0*, then f1*, then f2*. After the third call, reply "
               "with exactly the word DONE.")
PROMPT_BACKGROUND = ("Use the Bash tool with run_in_background set to true to run "
                     "exactly this command: sleep 20\n"
                     "Do not wait for it, do not check its output, and do not call any "
                     "other tool. As soon as it has started, reply with exactly the word "
                     "STARTED and end your turn.")

# 換算後的 token 最多可以比「這次叫用的頂層 usage」多這麼多。頂層 `usage` 只算主模型，
# 而換算用的 `modelUsage` 還包含 CLI 自己的小呼叫：2026-09-19 實測一次純聊天
# `usage.input_tokens` 10、`modelUsage` 914。倍數＋常數兩段都要：累計語意被誤當成每次
# 叫用時，第 3 次呼叫會是頂層的約 3 倍，這個容差擋得住。
_TOKEN_SLACK_RATIO = 1.5
_TOKEN_SLACK_ABS = 2000
# 工具回合：bot 的脈絡大小（最後一次 API 呼叫）至多是整次叫用加總的這個比例。實測
# 4 次 API 呼叫時 8,229／47,750≒0.17；兩次工具呼叫（3 次 API 呼叫）約 0.33。
_CONTEXT_MAX_SHARE = 0.75
# 登入類的驗證失敗文字（bare 模式在沒有 API key 的主機上就長這樣，見模組說明的表）。
# 「failed to authenticate」是 2026-09-19 實測的 401 那一句（「Failed to authenticate.
# API Error: 401 API key is invalid.」）——原本的字樣一個都不中。bot 那一側的判定
# （`_dorossi_cc_auth_failure`）另有自己的字樣與長度上限；兩邊對這兩句實測文字的答案
# 由 `test_verify_dorossi_cli` 用同一份語料對帳。
_LOGIN_ERROR_RE = re.compile(
    r"not logged in|please run /login|failed to authenticate|invalid api key|oauth token"
    r"|authentication",
    re.IGNORECASE)
# 來源標籤的形狀用 bot 那一份（bot 的啟動警告印同一個欄位），不另寫一支。
_API_KEY_SOURCE_SAFE = db._DOROSSI_API_KEY_SOURCE_LABEL_RE

# import 當下的正式帳本路徑——`_isolate_bot_state` 之後不能再是它。
_PRODUCTION_USAGE_FILE = db.DOROSSI_USAGE_FILE
_PRODUCTION_SESSION_FILE = db.DOROSSI_SESSION_FILE


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------
def _harden_console() -> None:
    """讓印不出來的字元降級成逃脫序列，而不是讓這支工具死在半路（理由同
    `verify_browser._harden_console`：這支的契約是一定要印出結論那一行）。只在
    `__main__` 呼叫，import 時不動別人的 stdout。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            continue


def _progress(msg: str) -> None:
    print(msg, flush=True)


def _console_safe(value, limit: int = 160) -> str:
    """外來字串（CLI 回的值）→ 單行、截短、純 ASCII（非 ASCII 字元轉成逃脫序列）。

    本檔自己寫的字面值由 `test_text_encoding` 靜態保證在 cp950 印得出來；外來的值靜態管
    不到，所以一律壓成 ASCII，不必靠主控台的編碼碰運氣。"""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[:limit] + "..."
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _result_line(verdict: str, checks: int, reason: str = "") -> str:
    line = f"{RESULT_PREFIX} {verdict} ({checks} checks)"
    if reason:
        line += " " + _console_safe(reason, 200)
    return line


def _emit(verdict: str, checks: int, reason: str = "") -> int:
    print(_result_line(verdict, checks, reason), flush=True)
    return {"OK": EXIT_OK, "FAIL": EXIT_FAIL, "SKIP": EXIT_SKIP}[verdict]


# --------------------------------------------------------------------------
# 一次 CLI 呼叫：原始輸出全部交給 bot 的折疊、判定與計帳
# --------------------------------------------------------------------------
@dataclass
class _Call:
    label: str
    prompt: str
    session_id: str | None = None        # 這次 `--resume` 的 id（新工作階段是 None）
    baseline: dict | None = None         # 上一次存下的 `usage_mark`（bot 存在工作階段槽裡）
    state: db._ClaudeStreamState = None  # type: ignore[assignment]
    init: dict | None = None
    rate_events: int = 0
    tool_use_ids: set = field(default_factory=set)
    max_background: int = 0
    rc: int | None = None
    err: str = ""
    verdict: str | None = None
    verdict_error: str | None = None
    info: dict | None = None
    accounting_error: str | None = None
    bot_diag: str = ""
    elapsed: float = 0.0
    timeout_sec: float = CALL_TIMEOUT_SEC

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = db._ClaudeStreamState(self.session_id)

    def feed(self, text: str) -> None:
        """一行 stdout：先交給 bot 的折疊，再記 bot 不讀的欄位。"""
        self.state.feed(text)
        self._observe(text)
        # 看門狗讀的就是這個集合；記下串流途中的最大值（結束時它通常又空了）。
        if self.state.background_tasks:
            self.max_background = max(self.max_background,
                                      len(self.state.background_tasks))

    def _observe(self, text: str) -> None:
        """只記 bot **不讀**的東西（警報用），不重做 bot 已經做的判讀。"""
        try:
            event = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and self.init is None:
            self.init = event
        elif kind == "rate_limit_event":
            self.rate_events += 1
        elif kind == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.tool_use_ids.add(str(block.get("id")))

    def finish(self, rc: int, err: str, timeout_sec: float) -> None:
        """串流結束之後：bot 的判定，判定通過才走 bot 的計帳（跟 bot 的順序一樣）。

        bot 的函式會往 stderr 印診斷；這裡收起來放進 `bot_diag`，失敗時才顯示。"""
        if _ledger_is_in_repo():
            raise RuntimeError("usage ledger is not redirected out of the repo")
        self.rc = rc
        self.err = err or ""
        self.timeout_sec = timeout_sec
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            try:
                self.verdict = db._claude_stream_verdict(
                    self.state, rc, self.err, self.session_id,
                    idle_limit=timeout_sec, hard_limit=timeout_sec)
            except Exception as exc:  # pylint: disable=broad-except
                self.verdict_error = type(exc).__name__
            if self.verdict is not None:
                try:
                    self.info = db._dorossi_round_info_and_record(
                        self.state.last_result_ev, stderr_tail=self.err,
                        cli_command=db._dorossi_cli_command_of(self.prompt),
                        resumed_id=self.session_id, sid=self.state.sid,
                        cli_version=self.state.cli_version, baseline=self.baseline)
                except Exception as exc:  # pylint: disable=broad-except
                    self.accounting_error = type(exc).__name__
        self.bot_diag = buffer.getvalue()

    @property
    def timed_out(self) -> bool:
        return self.state.kill_reason is not None

    def failure_words(self) -> str:
        """這次呼叫沒有正常收尾時的一句原因（型別名與旗標，不含原始錯誤文字）。"""
        if self.timed_out:
            return f"{self.label} 超過 {self.timeout_sec:g} 秒被砍掉"
        if self.verdict_error:
            return f"{self.label} 被 bot 的判定判為 {self.verdict_error}（rc={self.rc}）"
        if self.verdict is None:
            return f"{self.label} 沒有跑完"
        return ""

    def diag_lines(self) -> list[str]:
        """bot 自己印的診斷（最多兩行），給失敗的檢查附在後面。"""
        lines = [line for line in self.bot_diag.splitlines() if line.strip()]
        return [f"bot 的診斷：{_console_safe(line)}" for line in lines[:2]]


def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _run_call(call: _Call, argv: list, cwd: Path, env: dict,
                    timeout_sec: float = CALL_TIMEOUT_SEC) -> _Call:
    """起一個 CLI、把 prompt 從 stdin 送進去、逐行交給 `call.feed`，最後 `call.finish`。

    讀取迴圈只有一道牆鐘上限（驗證工具不需要 bot 的閒置 tier）。收尾一律走 bot 的
    `_dorossi_reap_proc` ／`_dorossi_drain_stderr`：`await proc.wait()` 與等 stderr 讀到
    EOF **各自**都是無限的。"""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        limit=_STREAM_LIMIT,
    )
    try:
        proc.stdin.write(call.prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except OSError:
        pass
    err_task = asyncio.ensure_future(db._read_stream_all(proc.stderr))
    deadline = started + timeout_sec
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                call.state.kill_reason = "hard"
                _kill(proc)
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                call.state.kill_reason = "hard"
                _kill(proc)
                break
            if not line:
                break
            call.feed(line.decode("utf-8", "replace").strip())
    except BaseException:
        _kill(proc)
        raise
    finally:
        rc = await db._dorossi_reap_proc(proc)
        err = await db._dorossi_drain_stderr(err_task)
    call.elapsed = time.monotonic() - started
    call.finish(db._DOROSSI_UNREAPED_RC if rc is None else rc, err, timeout_sec)
    return call


# --------------------------------------------------------------------------
# argv 與環境：bot 那一份，只換必要的地方
# --------------------------------------------------------------------------
def _model() -> str:
    return db.DOROSSI_MODEL_CHOICES[MODEL_KEY]


def pure_chat_argv(exe: str, session_id: str | None = None) -> list:
    """bot 的純聊天 argv（`tools_mode="off"`），只把模型換成小模型。"""
    return db._dorossi_cc_argv(exe, session_id=session_id, model=_model(),
                               tools_mode="off")


def tool_round_argv(exe: str, session_id: str, tool: str) -> list:
    """bot 的完整工具 argv（`tools_mode="full"`），後面再接一個只開 `tool` 的白名單。

    完整模式的 bot 不帶任何工具白名單；這裡多接的那一段只是把「模型能碰什麼」收斂到
    這一項檢查需要的那一個工具（`--allowedTools` 在略過核准的模式下是多餘的，留著是讓
    意圖寫在 argv 上）。"""
    return db._dorossi_cc_argv(exe, session_id=session_id, model=_model(),
                               tools_mode="full") + [
        "--tools", tool, "--allowedTools", tool]


def child_env(timeout_sec: float = CALL_TIMEOUT_SEC, *, pinned_exe: bool = False) -> dict:
    """bot 的子行程環境（`_dorossi_cc_child_env`），再拿掉互動式工作階段帶進來的變數。

    背景工作等待上限那個變數由 bot 的函式設（值＝`timeout_sec` 的毫秒數），會改變計費
    方式的憑證變數也由它拿掉，這裡都不再寫第二份——只負責「拿掉互動式工作階段的變數」
    與 `--exe` 的 `DISABLE_AUTOUPDATER`。"""
    env = db._dorossi_cc_child_env(timeout_sec)
    keep = db._DOROSSI_CC_BG_WAIT_CEILING_ENV.upper()
    for key in list(env):
        upper = key.upper()
        if upper == keep:
            continue
        if upper.startswith("CLAUDE") or upper == "AI_AGENT":
            del env[key]
    if pinned_exe:
        env["DISABLE_AUTOUPDATER"] = "1"
    return env


# --------------------------------------------------------------------------
# 不碰正式狀態
# --------------------------------------------------------------------------
def _inside_repo(path) -> bool:
    """`path` 落在 repo 底下（含 repo 本身）。解析不了就當成在裡面——失敗方向是不動手。"""
    try:
        target = Path(path).resolve()
        root = Path(db.PROJECT_ROOT).resolve()
    except OSError:
        return True
    return target == root or root in target.parents


def _ledger_is_in_repo() -> bool:
    """帳本路徑落在 repo 底下＝還沒導走（或導錯地方）。結構判準，不比對檔名。"""
    return _inside_repo(db.DOROSSI_USAGE_FILE)


def isolate_bot_state(state_dir: Path) -> None:
    """帳本與工作階段檔導到 `state_dir`。導不走就丟例外——寧可不驗，也不寫正式帳本。

    **先驗再動**：目標在 repo 底下時連目錄都不建、模組全域也不改。"""
    usage = Path(state_dir) / "usage.ndjson"
    session = Path(state_dir) / "session.json"
    if (_inside_repo(state_dir) or usage == _PRODUCTION_USAGE_FILE
            or session == _PRODUCTION_SESSION_FILE):
        raise RuntimeError("bot state could not be redirected out of the repo")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    for path in (usage, session):
        if path.exists():
            path.unlink()
    db.DOROSSI_USAGE_FILE = usage
    db.DOROSSI_SESSION_FILE = session


def scratch_dir(root: Path | None = None) -> Path:
    """repo 外的固定工作目錄；準備好 Glob 回合要找的三個小檔。"""
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    path = base / _SCRATCH_NAME
    if _inside_repo(path):
        raise RuntimeError("scratch directory would sit inside the repo")
    path.mkdir(parents=True, exist_ok=True)
    for name in _GLOB_FILES:
        (path / name).write_text("x", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 四項檢查（純函式：只讀 `_Call`，方便用假串流測）
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    number: int
    name: str
    ok: bool
    summary: str
    details: list = field(default_factory=list)

    def lines(self) -> list[str]:
        mark = " OK " if self.ok else "FAIL"
        out = [f"[{mark}] {self.number} {self.name}：{self.summary}"]
        out += [f"         - {detail}" for detail in self.details]
        return out


def _usage_total(result_ev, keys) -> int:
    usage = result_ev.get("usage") if isinstance(result_ev, dict) else None
    return db._dorossi_usage_int(usage, keys)


_ALL_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens", "output_tokens")
_CONTEXT_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens")


def check_pure_chat(call: _Call) -> CheckResult:
    """第 1 項：純聊天旗標下，工具列表是空的、答案與串流預覽都拿得到。"""
    problems: list[str] = []
    notes: list[str] = []
    state = call.state
    failure = call.failure_words()
    if failure:
        problems.append(failure)
    if call.init is None:
        problems.append("串流裡沒有 init 事件")
    else:
        tools = call.init.get("tools")
        if not isinstance(tools, list):
            problems.append("init 沒有工具列表（形狀變了）")
        elif tools:
            names = ", ".join(_console_safe(t, 40) for t in tools[:8])
            problems.append(
                f"純聊天旗標下 init 仍列出 {len(tools)} 個工具（{names}）——空白名單"
                "不再關得住工具集合，Dorossi 的「只能對話」不成立")
    if not db._claude_result_succeeded(state.last_result_ev):
        problems.append("最後的 result 不是成功完成（subtype／is_error 不對）")
    if not state.answer:
        problems.append("result.result 沒有答案文字")
    if not state.stream_text:
        problems.append("stream_event 的 text_delta 沒有累積出任何預覽文字")
    elif state.answer and state.stream_text.strip() != state.answer:
        problems.append("串流預覽文字與 result.result 不一致（text_delta 的累積規則可能變了）")
    if not state.sid:
        problems.append("讀不到工作階段 id（下一輪無法 resume）")
    if call.rate_events == 0:
        problems.append("沒有 rate_limit_event（撞到用量上限時只能猜重設時刻）")
    else:
        if state.rate_reset is None:
            problems.append("rate_limit_event 讀不出重設時刻")
        if state.rate_status not in db._DOROSSI_RATE_STATUSES:
            problems.append("rate_limit_event 的狀態不在已知詞彙裡（成功回合的否決會失效）")
    version = state.cli_version
    if db._dorossi_cc_version_tuple(version) is None:
        problems.append("init 的 claude_code_version 讀不到（計帳分不出每次叫用／累計）")
    notes.append(f"CLI 版本 {_console_safe(version)}，模型 {_console_safe(_model())}，"
                 f"耗時 {call.elapsed:.1f} 秒")
    notes.append(f"init 工具 {len(call.init.get('tools') or []) if call.init else '?'} 個，"
                 f"答案 {len(state.answer)} 字，配額狀態 {_console_safe(state.rate_status)}")
    if problems:
        return CheckResult(1, "純聊天旗標", False, problems[0],
                           problems[1:] + notes + call.diag_lines())
    return CheckResult(1, "純聊天旗標", True,
                       "工具列表是空的；答案、串流預覽、工作階段 id、配額事件、版本都讀得到",
                       notes)


def _tokens(info: dict) -> int:
    return sum(db._dorossi_count(info.get(k)) or 0 for k in ("in", "cr", "cc", "out"))


def check_accounting(calls: list, tool_call: _Call | None) -> CheckResult:
    """第 2 項：resume 之後每次叫用的數字不會變成累計、工具回合的脈絡大小是最後一次呼叫。

    `calls` 是同一個工作階段的純聊天呼叫（第 1 個是新開的），`tool_call` 是接在後面的
    Glob 回合。"""
    name = "逐次計帳"
    problems: list[str] = []
    notes: list[str] = []
    if not calls or not calls[0].state.sid or calls[0].info is None:
        return CheckResult(2, name, False, "沒有可以 resume 的工作階段（第 1 次呼叫沒成功）")
    sequence = list(calls) + ([tool_call] if tool_call is not None else [])
    previous_tokens = None
    for index, call in enumerate(sequence):
        failure = call.failure_words()
        if failure:
            problems.append(failure)
            continue
        if call.info is None:
            problems.append(f"{call.label} 的計帳沒有產出（{call.accounting_error}）")
            continue
        info = call.info
        mode = db._dorossi_cc_totals_mode(call.state.cli_version)
        acct = info.get("acct")
        cost = info.get("cost_usd")
        tokens = _tokens(info)
        floor = _usage_total(call.state.last_result_ev, _ALL_TOKEN_KEYS)
        raw = db._dorossi_cc_round_info(call.state.last_result_ev)
        notes.append(
            f"{call.label}：版本 {_console_safe(call.state.cli_version)}（{mode}），"
            f"原始 ${raw.get('cost_usd', 0.0):.6f}／{_tokens(raw)} token，"
            f"換算 ${cost:.6f}／{tokens} token（{acct}），頂層 usage {floor}")
        if index == 0:
            previous_tokens = tokens
            continue
        expected = {"per_call": "call", "cumulative": "delta"}.get(mode)
        if expected is None:
            problems.append(f"{call.label} 的版本讀不到，分不出總額語意")
        elif acct != expected:
            problems.append(
                f"{call.label} 的換算標籤是 {_console_safe(acct)}，{mode} 語意該是 "
                f"{expected}（基準鏈斷了，或 CLI 的總額語意又變了）")
        # 有限性要明問：`inf > 0` 為真，只比大小的話一個壞掉的無限大金額會被當成「正數」
        # 放行（bot 的換算本身已經擋掉非有限值，這裡是驗證工具不信任它的那一層）。
        if not (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and math.isfinite(cost) and cost > 0):
            problems.append(f"{call.label} 換算後的金額不是正數（${cost}）")
        if tokens > floor * _TOKEN_SLACK_RATIO + _TOKEN_SLACK_ABS:
            problems.append(
                f"{call.label} 換算後 {tokens} token，遠大於這次叫用的頂層 usage "
                f"{floor}——像是把工作階段累計當成了每次叫用")
        if (call is not tool_call and previous_tokens is not None
                and tokens > previous_tokens * _TOKEN_SLACK_RATIO + _TOKEN_SLACK_ABS):
            problems.append(
                f"{call.label} 的 token（{tokens}）比上一次（{previous_tokens}）大很多"
                "——每次叫用的數字不該逐輪長大")
        if call is not tool_call:
            previous_tokens = tokens
    if tool_call is not None and not tool_call.failure_words() and tool_call.info:
        problems += _context_problems(tool_call, notes)
    elif tool_call is None:
        problems.append("沒有跑工具回合，脈絡大小無從比對")
    if problems:
        return CheckResult(2, name, False, problems[0], problems[1:] + notes)
    return CheckResult(2, name, True,
                       "每次叫用的金額／token 不累計、不長大；脈絡大小取的是最後一次呼叫",
                       notes)


def _context_problems(call: _Call, notes: list) -> list[str]:
    """工具回合：`iterations` 最後一筆讀得到，bot 的脈絡大小遠小於整次叫用的加總。"""
    problems: list[str] = []
    ev = call.state.last_result_ev
    last = db._dorossi_last_call_context(ev)
    summed = _usage_total(ev, _CONTEXT_TOKEN_KEYS)
    context = db._dorossi_context_tokens(call.info)
    tools = len(call.tool_use_ids)
    notes.append(f"{call.label}：工具呼叫 {tools} 次，最後一次呼叫的脈絡 {last}，"
                 f"整次加總 {summed}，壓縮觸發用的脈絡 {context}")
    if last is None:
        problems.append("result.usage.iterations 的最後一筆讀不到——壓縮觸發會退回加總，"
                        "每一個工作輪後面都會跟一個壓縮輪（§8.71 的事故）")
    elif context != last:
        problems.append(f"壓縮觸發用的脈絡（{context}）不是最後一次呼叫的脈絡（{last}）")
    if tools == 0:
        problems.append("工具回合一次工具都沒呼叫，比不出「最後一次」與「加總」的差別")
    elif summed and context > summed * _CONTEXT_MAX_SHARE:
        problems.append(
            f"工具呼叫 {tools} 次，脈絡（{context}）卻接近整次加總（{summed}）"
            "——量到的是加總，不是最後一次呼叫")
    return problems


def check_background(call: _Call) -> CheckResult:
    """第 3 項：背景工作會出現在 `background_tasks`，串流自己以成功的 result 收尾。"""
    name = "背景工作訊號"
    problems: list[str] = []
    failure = call.failure_words()
    if failure:
        problems.append(failure)
    tools = call.init.get("tools") if call.init else None
    if isinstance(tools, list) and "Bash" not in tools:
        problems.append("init 沒有 Bash 工具（這台主機的 CLI 可能找不到 bash）")
    if call.max_background == 0:
        problems.append(
            "整個串流裡 background_tasks 一直是空的——看門狗的閒置／沉默抑制拿不到背景"
            "工作訊號（system/background_tasks_changed 的形狀可能變了）")
    if not db._claude_result_succeeded(call.state.last_result_ev):
        problems.append("串流沒有以成功的 result 收尾")
    notes = [f"背景工作最多 {call.max_background} 個，收尾時 "
             f"{len(call.state.background_tasks)} 個，耗時 {call.elapsed:.1f} 秒，"
             f"rc={call.rc}"]
    if problems:
        return CheckResult(3, name, False, problems[0],
                           problems[1:] + notes + call.diag_lines())
    return CheckResult(3, name, True,
                       "背景工作出現在 background_tasks，串流自己以成功的 result 收尾",
                       notes)


def bare_mode_findings(init: dict | None, result_ev: dict | None) -> list[str]:
    """這次工作階段像不像 `--bare`？回一串理由，空＝不像。純函式。

    判準見模組說明那張量測表。三條各自獨立成立，任一條就夠。

    來源標籤若正好是 bot **不交給子行程**的那幾個變數之一
    （`dorossi_backend._DOROSSI_CC_DROPPED_ENV`），值一定來自 CLI 自己的設定——那句話
    比「這個環境設了它」有用（原本的 `api_key_in_env` 參數在 builder 拿掉這些變數之後
    永遠是假的，所以換成這一條）。"""
    findings: list[str] = []
    if not isinstance(init, dict):
        return ["串流裡沒有 init 事件，無從判斷"]
    if "memory_paths" not in init:
        findings.append("init 沒有 memory_paths——指示檔／自動記憶的探索被關掉了"
                        "（--bare 就是這個樣子）")
    source = init.get("apiKeySource")
    if source not in (None, "none"):
        # 這個欄位是 CLI 的來源標籤（例如 `ANTHROPIC_API_KEY`），不是金鑰本身；形狀不像
        # 標籤就不印，免得哪天欄位語意變了而把值帶出來。
        shown = (source if isinstance(source, str)
                 and _API_KEY_SOURCE_SAFE.fullmatch(source) else "（形狀不明的值）")
        stripped = (isinstance(source, str)
                    and source.upper() in db._DOROSSI_CC_DROPPED_ENV)
        extra = ("（bot 不把這個變數交給子行程，所以它來自 CLI 自己的設定，例如設定檔的"
                 " env 區塊）" if stripped else "")
        findings.append(
            f"驗證來源是 {shown}，不是登入{extra}——計費不再走登入方案，bot 的設計前提"
            "不成立（--bare 只讀 API key）")
    if isinstance(result_ev, dict) and result_ev.get("is_error"):
        text = result_ev.get("result")
        if isinstance(text, str) and _LOGIN_ERROR_RE.search(text):
            findings.append(f"result 是登入類的驗證錯誤：{_console_safe(text, 80)}")
    return findings


def check_bare_mode(call: _Call) -> CheckResult:
    """第 4 項：第 1 次呼叫的 init／result 不像 bare 模式。"""
    name = "不是 bare 模式"
    findings = bare_mode_findings(call.init, call.state.last_result_ev)
    init = call.init if isinstance(call.init, dict) else {}
    memory = init.get("memory_paths")
    keys = sorted(memory) if isinstance(memory, dict) else memory
    notes = [f"apiKeySource={_console_safe(init.get('apiKeySource'))}，"
             f"memory_paths 的鍵={_console_safe(keys)}"]
    if findings:
        return CheckResult(4, name, False,
                           "這個工作階段像是 --bare 模式：" + findings[0],
                           findings[1:] + notes)
    return CheckResult(4, name, True, "驗證走登入、指示檔探索開著", notes)


# --------------------------------------------------------------------------
# 流程
# --------------------------------------------------------------------------
async def _verify(exe: str, cwd: Path, env: dict,
                  timeout_sec: float = CALL_TIMEOUT_SEC) -> list[CheckResult]:
    results: list[CheckResult] = []

    _progress("  ... 呼叫 1/5：純聊天旗標、新工作階段")
    first = await _run_call(_Call("第 1 次（新開）", PROMPT_PING),
                            pure_chat_argv(exe), cwd, env, timeout_sec)
    result = check_pure_chat(first)
    results.append(result)
    for line in result.lines():
        _progress(line)

    resumed: list[_Call] = [first]
    tool_call = None
    if first.state.sid and first.info is not None:
        sid, mark = first.state.sid, first.info.get("usage_mark")
        for number, prompt in ((2, PROMPT_TWO), (3, PROMPT_THREE)):
            _progress(f"  ... 呼叫 {number}/5：純聊天旗標、resume 同一個工作階段")
            call = await _run_call(
                _Call(f"第 {number} 次（resume）", prompt, sid, mark),
                pure_chat_argv(exe, sid), cwd, env, timeout_sec)
            resumed.append(call)
            if call.info is None or not call.state.sid:
                break
            sid, mark = call.state.sid, call.info.get("usage_mark")
        else:
            _progress("  ... 呼叫 4/5：只開 Glob 的工具回合、resume 同一個工作階段")
            tool_call = await _run_call(
                _Call("第 4 次（Glob）", PROMPT_GLOB, sid, mark),
                tool_round_argv(exe, sid, "Glob"), cwd, env, timeout_sec)
    result = check_accounting(resumed, tool_call)
    results.append(result)
    for line in result.lines():
        _progress(line)

    _progress("  ... 呼叫 5/5：只開 Bash 的工具回合、新工作階段、背景跑 sleep 20")
    background = await _run_call(_Call("第 5 次（背景）", PROMPT_BACKGROUND),
                                 tool_round_argv(exe, None, "Bash"), cwd, env,
                                 timeout_sec)
    result = check_background(background)
    results.append(result)
    for line in result.lines():
        _progress(line)

    result = check_bare_mode(first)
    results.append(result)
    for line in result.lines():
        _progress(line)
    return results


def summarize(results: list) -> tuple[str, str]:
    """全部檢查 → (結論, 原因)。"""
    failed = [str(r.number) for r in results if not r.ok]
    if failed:
        return "FAIL", "failed: " + ",".join(failed)
    return "OK", ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_dorossi_cli.py",
        description=("用 bot 自己的解析與計帳程式碼，實際打一次後端 CLI，驗證 bot 依賴的"
                     "串流契約（純聊天工具列表、逐次計帳、背景工作訊號、非 bare 模式）。"
                     "會花一點錢（小模型，約美金幾分錢）。"))
    parser.add_argument(
        "--exe", metavar="PATH",
        help=("改驗這一份 CLI 執行檔（例如隔離安裝的新版本）；會對它設定 "
              "DISABLE_AUTOUPDATER=1。預設跟 bot 一樣從 PATH 找 claude。"))
    return parser


def main(argv: list[str] | None = None) -> int:
    # `argv=None` ＝沒有旗標，不是去讀 `sys.argv`（本專案約定，見 `verify_browser.main`）。
    parser = _build_parser()
    args = parser.parse_args(argv or [])
    if args.exe:
        if not Path(args.exe).is_file():
            parser.error("--exe 指到的不是一個存在的檔案")
        exe = args.exe
    else:
        exe = shutil.which("claude")
        if exe is None:
            return _emit("SKIP", 0, "claude not found on PATH")
    try:
        cwd = scratch_dir()
        isolate_bot_state(cwd / "_state")
        env = child_env(pinned_exe=bool(args.exe))
        _progress("後端 CLI 串流契約驗證開始（" + ("指定的執行檔" if args.exe else "PATH 上的 claude")
                  + "，工作目錄在系統暫存目錄、帳本已導走）")
        results = asyncio.run(_verify(exe, cwd, env))
    except Exception as err:  # pylint: disable=broad-except
        return _emit("FAIL", 0, f"unexpected {type(err).__name__}")
    verdict, reason = summarize(results)
    return _emit(verdict, len(results), reason)


if __name__ == "__main__":
    _harden_console()
    sys.exit(main(sys.argv[1:]))
