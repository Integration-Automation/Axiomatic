"""Local axiomatic operations dashboard.

Run:
    py -3 axiomatic/dashboard_server.py

It serves a small HTML dashboard at http://127.0.0.1:8765/ and JSON at
/api/status. Stdlib-only, read-only, local bind by default.
"""
from __future__ import annotations

import ipaddress
import json
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from _bot_config import load_bot_config
    from _process_control import _pid_alive
    from _platform_runtime import (
        STATE_ROOT, active_platform, normalise_platform,
        platform_file as _platform_state)
    from _run_progress import read_progress
except ImportError:
    from axiomatic._bot_config import load_bot_config  # type: ignore
    from axiomatic._process_control import _pid_alive  # type: ignore
    from axiomatic._platform_runtime import (  # type: ignore
        STATE_ROOT, active_platform, normalise_platform,
        platform_file as _platform_state)
    from axiomatic._run_progress import read_progress  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 批次那一側的檔案是**全機共用**的（一份批次、一組佇列），bot 那一側的狀態則是
# **逐平台**的。儀表板不帶平台旗標時看的是預設平台那一份——它是唯讀的觀測工具，
# 要看別的平台就用 `--platform` 或 `AXIOMATIC_PLATFORM` 起第二份。
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
DOROSSI_EVENTS_FILE = _platform_state(PROJECT_ROOT / "dorossi_events.ndjson")
DOROSSI_QUEUE_FILE = _platform_state(PROJECT_ROOT / "dorossi_queue.ndjson")
DOROSSI_FAILED_QUEUE_FILE = _platform_state(
    PROJECT_ROOT / "dorossi_queue_failed.ndjson")
GENERATE_HISTORY_FILE = _platform_state(
    PROJECT_ROOT / "generate_history.ndjson")
WEBRUNNER_LOG = PROJECT_ROOT / "webrunner.log"
WEBRUNNER_PID_FILE = PROJECT_ROOT / "webrunner.pid"
WEBRUNNER_PAUSE_FILE = PROJECT_ROOT / "webrunner.pause"
BATCH_LABEL_FILE = PROJECT_ROOT / "batch_label.txt"
OUTPUT_ROOT = PROJECT_ROOT / "output"
# 批次的四個佇列檔。角色二是**位置對應**的：空行代表「這一對拿掉角色二」，所以算行數、
# 不略過空行；另外三個空行不算一筆（與 `read_todo_entries` 同一套規則）。
QUEUE_FILES = {
    "prompt": (PROJECT_ROOT / "todo_prompt.md", False),
    "char1": (PROJECT_ROOT / "todo_character1.md", False),
    "char2": (PROJECT_ROOT / "todo_character2.md", True),
    "undesired": (PROJECT_ROOT / "todo_undesired.md", False),
}


def _read_ndjson_tail(path: Path, n: int = 20) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows[-n:]


def _count_ndjson(path: Path) -> int:
    """Count NDJSON object rows in `path`.

    `_read_ndjson_tail` truncates to its last `n` rows, so it must NOT be used
    to measure a queue depth: the Dorossi queue files are rewritten whole
    (`_dorossi_queue_write` in the bot), so their row count IS the pending
    depth, and a tail-capped count silently under-reports once the queue grows
    past the cap.
    """
    if not path.exists():
        return 0
    total = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    total += 1
    except OSError:
        return 0
    return total


def _read_pid() -> tuple[int | None, bool]:
    """回 `(pid, 判定得出來嗎)`。**看板不得把「讀不出來」講成「沒有在跑」。**

    `webrunner.pid` 的**把關用讀取端**有四支——`verify_browser._live_webrunner_pid`、
    `start_webrunner._live_webrunner_pid`、`discord_bot._load_pid`，以及這一支——
    四支都各自寫了一次讀取邏輯，而且在 2026-09-07 之前四支都犯同一個錯：把「檔案
    不存在」與「檔案在但讀不出來」混成同一個答案。

    **這一段以前寫的是「目前有四個獨立的讀取端」，那個數字是錯的（2026-09-21 修）。**
    用 AST 掃過整份產品碼，讀這個檔的函式有**八個**：上面四支，加上
    `run_batch._bot_spawned_pid`（唯讀前置檢查，判不出來時樂觀往下跑是安全的，因為
    真正把關的是它交棒的 `start_webrunner.py` 子行程）、
    `_webrunner_shared.claim_liveness_signal`（問的是「需不需要我來認領」，回
    `int | None`），以及兩支擁有權比對（`start_webrunner._clear_pid_if_ours` 與
    `_webrunner_shared.release_liveness_signal`，問的是「檔案裡還記著我寫的那個 pid
    嗎」）。後面那四支刻意不是三分法，各自都寫了理由——錯的不是它們，是這句話連同
    另外三處文件都在說「第五個要遵守同樣的三分法」，而第五到第八個早就在了。
    分類與雙向對帳現在住在 `test/test_pid_file_readers.py`：掃到卻沒分類會紅，
    清單裡留著已經不讀這個檔的函式也會紅，四支把關讀取端還會被放到同一組檔案狀態
    語料上對拉。

    這一支的後果最輕——它只餵唯讀看板、不驅動任何動作——但方向仍然是錯的：看板
    存在的理由就是回答「現在有沒有在跑」，而「我讀不到那個檔」跟「沒有在跑」是兩
    件完全不同的事。顯示成後者會讓看的人以為批次停了。

    **不要**在這裡刪檔或做任何修復動作。這是唯讀的顯示層，別的行程正靠這個檔互相
    協調（`discord_bot._load_pid` 就是因為在讀不出來時 `unlink` 而變成這一族裡最
    嚴重的一個：它會把別人有效的存活訊號毀掉）。
    """
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, True          # 沒有這個檔＝真的沒有在跑
    except (OSError, UnicodeDecodeError):
        return None, False         # 檔案在、但讀不出來＝判不出來
    try:
        return int(raw), True
    except ValueError:
        return None, False         # 內容不是數字＝判不出來


def _safe_mtime(path: Path) -> float | None:
    """`exists()` + `stat()` is a TOCTOU race against the live writers (the
    log is appended to and the label file is rewritten while we serve), so
    stat directly and treat any OSError as "not there"."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _dir_image_count(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                total += 1
    except OSError:
        # Tree mutated underneath the walk (the generator writes into it while
        # we read) — report what we counted rather than 500-ing the request.
        return total
    return total


def _queue_depth(path: Path, positional: bool) -> int | None:
    """佇列檔還剩幾筆；檔案不在＝0，讀不出來＝None（看板顯示「—」，不假裝是 0）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    lines = text.splitlines()
    return len(lines) if positional else sum(1 for line in lines if line.strip())


def _progress_summary() -> dict:
    """目前角色的進度：資料夾名、已存張數、目標張數。

    **刻意不帶提示詞**：檢查點裡存著整段提示詞與角色字串，看板只需要「做到哪裡」，
    不需要把那些文字端出去（這個頁面沒有認證，綁錯位址時整個網段都讀得到）。
    """
    progress = read_progress() or {}
    out = {}
    folder = progress.get("folder")
    if isinstance(folder, str):
        out["folder"] = folder[:120]
    for key in ("saved", "target"):
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def _system_summary() -> dict:
    """磁碟剩餘（GB，專案所在的磁碟）與記憶體使用率；拿不到的欄位是 None。"""
    try:
        disk_free_gb = round(shutil.disk_usage(PROJECT_ROOT).free / 1e9, 1)
    except OSError:
        disk_free_gb = None
    try:
        import psutil  # type: ignore  # noqa: PLC0415  # 必要相依，但看板不該因為它而起不來
        memory_percent = round(float(psutil.virtual_memory().percent), 1)
    except (ImportError, OSError, AttributeError, ValueError):
        memory_percent = None
    return {"disk_free_gb": disk_free_gb, "memory_percent": memory_percent}


def _hostname_of(raw: str) -> str:
    """從 `Host` 標頭取出主機名稱，去掉埠號與 IPv6 的方括號。

    只在「冒號後面全是數字」時才把它當埠號切掉——IPv6 字面值沒加方括號時
    （`::1`，格式上不合法但別人送得出來）不能被切成 `:`，那會把一個 IP
    字面值變成空字串，判斷結果就反了。
    """
    value = (raw or "").strip()
    if value.startswith("["):                 # `[::1]:8765` / `[::1]`
        end = value.find("]")
        return value[1:end] if end > 0 else value.lstrip("[")
    head, sep, tail = value.rpartition(":")
    if sep and head and tail.isdigit() and ":" not in head:
        return head
    return value


def _host_header_is_local(raw: str, extra: frozenset) -> bool:
    """`Host` 標頭可不可信——擋 DNS rebinding 用。

    判準是「**是不是一個名字**」，不是「是不是 loopback」：rebinding 必須靠
    一個攻擊者控制得了的 DNS 名稱（瀏覽器送出的 Host 會是 `evil.com:8765`），
    所以 IP 字面值一律放行、名字一律擋掉。這樣連刻意綁在區域網路 IP 的用法
    也照樣可用，不需要為了安全犧牲那個設定。

    兩個例外：`localhost` 是 RFC 6761 的特例名稱，解析由作業系統／瀏覽器內建，
    攻擊者搶不到；`extra` 是使用者自己寫在 `bot_config.json` 的 `dashboard.host`，
    他既然指定了那個名字，用它連進來就是預期用法。

    **沒有 `Host` 標頭一律擋掉**（fail-closed，與 `_is_loopback` 同一個立場）。
    瀏覽器一定會送，送不出來的不是這個儀表板要服務的對象。
    """
    name = _hostname_of(raw).lower()
    if not name:
        return False
    if name == "localhost" or name in extra:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    """`host` 綁的是不是只有本機連得到的位址。

    空字串與 `"0.0.0.0"` / `"::"` 都是**所有介面**，是最容易誤設的兩個值；
    `ip_address` 認不得的字串（主機名稱）一律當成非 loopback（fail-closed，
    寧可多吵一次）。
    """
    h = (host or "").strip().strip("[]")
    if not h or h in ("0.0.0.0", "::", "*"):
        return False
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def known_platforms() -> list[str]:
    """這台機器上有狀態的平台（`state/<名稱>/` 存在），加上這個行程自己的平台。

    只收 `normalise_platform` 認得的名字，所以 `?platform=` 帶進來的字串不可能變成
    任意路徑：不在這份清單裡的值一律退回這個行程的平台。
    """
    names = {active_platform()}
    try:
        for child in STATE_ROOT.iterdir():
            if child.is_dir() and normalise_platform(child.name) == child.name:
                names.add(child.name)
    except OSError:
        pass
    return sorted(names)


def _platform_files(platform: str) -> dict:
    """某個平台自己那一份的檔案。這個行程的平台直接用模組常數（測試會替換它們）。"""
    if platform == active_platform():
        return {"dorossi_queue": DOROSSI_QUEUE_FILE,
                "dorossi_failed": DOROSSI_FAILED_QUEUE_FILE,
                "dorossi_events": DOROSSI_EVENTS_FILE,
                "generate_history": GENERATE_HISTORY_FILE}
    return {
        "dorossi_queue": _platform_state(
            PROJECT_ROOT / "dorossi_queue.ndjson", platform=platform),
        "dorossi_failed": _platform_state(
            PROJECT_ROOT / "dorossi_queue_failed.ndjson", platform=platform),
        "dorossi_events": _platform_state(
            PROJECT_ROOT / "dorossi_events.ndjson", platform=platform),
        "generate_history": _platform_state(
            PROJECT_ROOT / "generate_history.ndjson", platform=platform),
    }


def build_status(platform: str | None = None) -> dict:
    """儀表板的狀態。`platform` 只換「每個平台各一份」的那幾塊；批次全機一份，不跟著換。"""
    platforms = known_platforms()
    chosen = platform if platform in platforms else active_platform()
    files = _platform_files(chosen)
    pid, pid_known = _read_pid()
    log_mtime = _safe_mtime(WEBRUNNER_LOG)
    # `alive` 刻意是**三態**：True／False／None。None ＝「pid 檔讀不出來，判不
    # 出來」。原本這裡是 `_pid_alive(pid or 0)`，於是讀不出來會塌成 False，看板
    # 就顯示「stopped」——把「我不知道」講成「沒有在跑」，正好是這個看板最不該
    # 說的那句話。前端要跟著做三態判斷，否則這裡分好的資訊會在最後一步又被丟掉。
    if pid is not None:
        alive = _pid_alive(pid)
    else:
        alive = False if pid_known else None
    return {
        "ts": time.time(),
        "label": _safe_text(BATCH_LABEL_FILE),
        "webrunner": {
            "pid": pid,
            "alive": alive,
            "paused": WEBRUNNER_PAUSE_FILE.exists(),
            "log_age_sec": (time.time() - log_mtime) if log_mtime else None,
        },
        "dorossi": {
            "queue": _count_ndjson(files["dorossi_queue"]),
            "failed_queue": _count_ndjson(files["dorossi_failed"]),
            "events": _read_ndjson_tail(files["dorossi_events"], 10),
        },
        "generate": {
            "history": _read_ndjson_tail(files["generate_history"], 10),
        },
        "events": _read_ndjson_tail(EVENTS_FILE, 10),
        "output_images": _dir_image_count(OUTPUT_ROOT),
        "progress": _progress_summary(),
        "queues": {name: _queue_depth(path, positional)
                   for name, (path, positional) in QUEUE_FILES.items()},
        "system": _system_summary(),
        # 這一份狀態讀的是哪一個平台；`platforms` 是頁首下拉選單的選項。
        "platform": chosen,
        "platforms": platforms,
    }


HTML = """<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Axiomatic 儀表板</title>
<style>
:root{--bg:#f4f6f8;--panel:#ffffff;--line:#d8dee5;--text:#18202a;--muted:#5d6b7a;
  --accent:#2563eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--code:#eef1f4}
@media (prefers-color-scheme: dark){:root{--bg:#0f141a;--panel:#171e26;--line:#2a3441;
  --text:#e6ebf0;--muted:#93a2b3;--accent:#60a5fa;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--code:#0b1015}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
header{position:sticky;top:0;z-index:1;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;
  padding:12px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;flex:1 1 auto}
.meta{color:var(--muted);font-size:13px}
button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
main{max-width:1180px;margin:0 auto;padding:16px;display:grid;gap:16px;
  grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
section.wide{grid-column:1/-1}
section h2{font-size:14px;margin:0 0 10px;color:var(--muted);letter-spacing:.04em}
.rows{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;align-items:baseline}
.rows dt{color:var(--muted)}
.rows dd{margin:0;font-weight:600;min-width:0;overflow-wrap:anywhere}
.pill{display:inline-block;padding:1px 10px;border-radius:999px;font-weight:700;font-size:13px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.bar{height:8px;border-radius:4px;background:var(--code);overflow:hidden;margin-top:4px}
.bar>span{display:block;height:100%;background:var(--accent);width:0}
ol.events{list-style:none;margin:0;padding:0;display:grid;gap:4px;max-height:320px;overflow:auto}
ol.events li{display:grid;grid-template-columns:auto auto 1fr;gap:8px;font-size:13px;
  padding:4px 0;border-bottom:1px solid var(--line);min-width:0}
ol.events time{color:var(--muted);font-variant-numeric:tabular-nums}
ol.events b{font-weight:600}
ol.events span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:var(--code);border-radius:8px;
  padding:12px;max-height:360px;overflow:auto;font-size:12px}
#platform{font:inherit;padding:2px 6px;border-radius:6px;border:1px solid var(--line);
  background:var(--panel);color:var(--text)}
details summary{cursor:pointer;color:var(--muted)}
</style>
<header>
  <h1>Axiomatic 儀表板</h1>
  <label class="meta">平台 <select id="platform" aria-label="平台"></select></label>
  <span class="meta">更新於 <span id="updated">—</span></span>
  <label class="meta"><input type="checkbox" id="auto" checked> 每 15 秒自動更新</label>
  <button id="refresh" type="button">重新整理</button>
</header>
<main>
  <section>
    <h2>批次</h2>
    <dl class="rows">
      <dt>狀態</dt><dd><span id="batch-state" class="pill muted">—</span></dd>
      <dt>批次標籤</dt><dd id="batch-label">—</dd>
      <dt>目前角色</dt><dd id="batch-folder">—</dd>
      <dt>進度</dt><dd><span id="batch-progress">—</span><div class="bar"><span id="batch-bar"></span></div></dd>
      <dt>log 最後更新</dt><dd id="batch-log">—</dd>
    </dl>
  </section>
  <section>
    <h2>佇列</h2>
    <dl class="rows">
      <dt>提示詞</dt><dd id="q-prompt">—</dd>
      <dt>角色一</dt><dd id="q-char1">—</dd>
      <dt>角色二（位置對應）</dt><dd id="q-char2">—</dd>
      <dt>負面提示詞</dt><dd id="q-undesired">—</dd>
    </dl>
  </section>
  <section>
    <h2>Dorossi</h2>
    <dl class="rows">
      <dt>等待中的提問</dt><dd id="d-queue">—</dd>
      <dt>還原失敗</dt><dd id="d-failed">—</dd>
    </dl>
    <ol class="events" id="d-events"></ol>
  </section>
  <section>
    <h2>系統</h2>
    <dl class="rows">
      <dt>輸出圖片</dt><dd id="s-images">—</dd>
      <dt>磁碟剩餘</dt><dd id="s-disk">—</dd>
      <dt>記憶體使用</dt><dd id="s-mem">—</dd>
    </dl>
  </section>
  <section class="wide">
    <h2>最近的批次事件</h2>
    <ol class="events" id="events"></ol>
  </section>
  <section class="wide">
    <details><summary>原始狀態（JSON）</summary><pre id="raw">loading...</pre></details>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function setText(id, value){ $(id).textContent = (value === null || value === undefined || value === '') ? '—' : String(value); }
function clock(ts){ if(!ts) return '—'; const d = new Date(ts * 1000); return d.toLocaleTimeString('zh-TW', {hour12:false}); }
function ago(sec){
  if(sec === null || sec === undefined) return '—';
  if(sec < 90) return Math.round(sec) + ' 秒前';
  if(sec < 5400) return Math.round(sec / 60) + ' 分鐘前';
  return (sec / 3600).toFixed(1) + ' 小時前';
}
// 三態：alive 為 null＝pid 檔讀不出來、判斷不出，絕不顯示成「已停止」。
function batchState(webrunner){
  if(webrunner.alive === null) return ['判斷不出（pid 檔讀不到）', 'warn'];
  if(!webrunner.alive) return ['已停止', 'bad'];
  if(webrunner.paused) return ['已暫停', 'warn'];
  return ['執行中', 'ok'];
}
function renderEvents(listId, events){
  const items = (events || []).slice().reverse().map((event) => {
    const li = document.createElement('li');
    const t = document.createElement('time'); t.textContent = clock(event.ts);
    const b = document.createElement('b'); b.textContent = event.type || '?';
    const rest = Object.assign({}, event); delete rest.ts; delete rest.type;
    const s = document.createElement('span'); s.textContent = JSON.stringify(rest);
    s.title = s.textContent;
    li.append(t, b, s); return li;
  });
  $(listId).replaceChildren(...items);
}
function render(s){
  const [label, tone] = batchState(s.webrunner);
  const pill = $('batch-state'); pill.textContent = label; pill.className = 'pill ' + tone;
  setText('batch-label', s.label);
  const p = s.progress || {};
  setText('batch-folder', p.folder);
  const saved = Number(p.saved || 0), target = Number(p.target || 0);
  setText('batch-progress', target ? saved + ' / ' + target : '');
  $('batch-bar').style.width = target ? Math.min(100, saved * 100 / target) + '%' : '0';
  setText('batch-log', ago(s.webrunner.log_age_sec));
  const q = s.queues || {};
  for(const key of ['prompt', 'char1', 'char2', 'undesired']) setText('q-' + key, q[key]);
  setText('d-queue', s.dorossi.queue); setText('d-failed', s.dorossi.failed_queue);
  renderEvents('d-events', s.dorossi.events);
  renderEvents('events', s.events);
  const sys = s.system || {};
  setText('s-images', s.output_images);
  setText('s-disk', sys.disk_free_gb === null || sys.disk_free_gb === undefined ? '' : sys.disk_free_gb + ' GB');
  setText('s-mem', sys.memory_percent === null || sys.memory_percent === undefined ? '' : sys.memory_percent + '%');
  setText('updated', clock(s.ts));
  renderPlatforms(s.platforms || [], s.platform);
  $('raw').textContent = JSON.stringify(s, null, 2);
}
let chosenPlatform = '';
function renderPlatforms(names, current){
  const select = $('platform');
  if(select.dataset.names !== names.join(',')){
    select.replaceChildren(...names.map(name => {
      const option = document.createElement('option');
      option.value = name; option.textContent = name;
      return option;
    }));
    select.dataset.names = names.join(',');
  }
  select.value = current || '';
  chosenPlatform = current || '';
}
async function load(){
  try {
    const query = chosenPlatform ? '?platform=' + encodeURIComponent(chosenPlatform) : '';
    const r = await fetch('/api/status' + query, {cache: 'no-store'});
    render(await r.json());
  } catch (error) {
    setText('updated', '讀取失敗，稍後重試');
  }
}
$('refresh').addEventListener('click', load);
$('platform').addEventListener('change', event => { chosenPlatform = event.target.value; load(); });
setInterval(() => { if($('auto').checked) load(); }, 15000);
load();
</script>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    # 第二道防線。頁面的 script／style 都是行內的，所以 CSP 沒辦法直接封掉
    # 行內執行；但 `default-src 'none'` ＋ `connect-src 'self'` 封掉的是**外送
    # 管道**——注進來的東西即使跑起來了，也 fetch 不出去、也載不了外部圖片當
    # 信標。真正的修正是不要用 innerHTML 內插（見下面的 `card()`），這裡只是
    # 萬一哪天又有人寫回去時的護欄。
    _SECURITY_HEADERS = (
        ("Content-Security-Policy",
         "default-src 'none'; script-src 'unsafe-inline'; "
         "style-src 'unsafe-inline'; connect-src 'self'; "
         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    )

    # `main()` 會把設定檔裡綁的位址填進來，讓刻意用主機名稱連進來的人不被擋。
    # 預設空集合＝只認 IP 字面值與 `localhost`。
    extra_allowed_hosts: frozenset = frozenset()

    # 已經抱怨過的 Host，有上限。攻擊者發得出無限多個請求，所以這個集合
    # **必須**封頂——否則這行診斷自己就變成灌爆 log 的管道。
    _refused_hosts: set = set()
    _REFUSED_LOG_LIMIT = 5

    def _host_is_allowed(self) -> bool:
        raw = self.headers.get("Host")
        if _host_header_is_local(raw, self.extra_allowed_hosts):
            return True
        # 擋掉卻不說為什麼，使用者只會看到一片空白的頁面——而「刻意綁 `0.0.0.0`
        # 再用主機名稱連」正是會撞上這裡的合法用法。說一次就好。
        key = (raw or "")[:80]
        if (key not in self._refused_hosts
                and len(self._refused_hosts) < self._REFUSED_LOG_LIMIT):
            self._refused_hosts.add(key)
            # `!r`：`Host` 是對方控制的字串，而 stderr 會進 log。原樣印的話
            # 裡面的換行可以偽造出一整行假的 log。
            print(f"dashboard: refused a request whose Host header is "
                  f"{key!r} — only IP literals, `localhost`, and the "
                  f"configured `dashboard.host` are accepted (this blocks "
                  f"DNS rebinding). If you reach the dashboard by hostname, "
                  f"put that hostname in `dashboard.host`.", file=sys.stderr)
        return False

    def _send(self, code: int, ctype: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in self._SECURITY_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        # Anything raising out of here reaches socketserver's handle_error,
        # which dumps a traceback and drops the connection — the page then
        # just stops updating with no clue why. Answer 500 instead.
        try:
            if not self._host_is_allowed():
                # **不要把 Host 的內容回顯**——那是攻擊者控制的字串，而這個
                # 回應會被他讀走。固定字串就好。
                self._send(403, "application/json; charset=utf-8",
                           b'{"error": "host not allowed"}')
                return
            if self.path.startswith("/api/status"):
                query = parse_qs(urlsplit(self.path).query)
                wanted = (query.get("platform") or [None])[0]
                data = json.dumps(
                    build_status(wanted), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", data)
                return
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        except OSError:
            raise  # client hung up mid-write; nothing useful to send
        except Exception as error:  # pylint: disable=broad-except
            print(f"dashboard: request failed: {error!r}", file=sys.stderr)
            body = json.dumps({"error": "status unavailable"}).encode("utf-8")
            try:
                self._send(500, "application/json; charset=utf-8", body)
            except OSError:
                pass


def main() -> int:
    cfg = load_bot_config().get("dashboard", {})
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 8765)
    if not _is_loopback(host):
        # 這個儀表板**沒有任何認證**，而它端出去的東西正是 Layer 1 禁止送進
        # 對話平台的那一類：PID、log 年齡、佇列內容、Dorossi 事件（含使用者
        # 提示詞）、批次標籤。綁在 loopback 以外就等於把這些交給整個網段。
        # 不擋（使用者可能是刻意的），但要吵。
        print(f"dashboard: WARNING — binding to {host!r}, not loopback. "
              f"There is no authentication; anyone who can reach this port "
              f"sees PIDs, queue contents and recent events. Set "
              f"dashboard.host to 127.0.0.1 in bot_config.json unless this "
              f"is deliberate. Note requests are still refused unless their "
              f"Host header is an IP literal, `localhost`, or {host!r} — "
              f"reaching this by any other hostname needs that name in "
              f"dashboard.host.", file=sys.stderr)
    configured = (host or "").strip().strip("[]").lower()
    Handler.extra_allowed_hosts = frozenset({configured} if configured else ())
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard listening on http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard: Ctrl+C — stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
