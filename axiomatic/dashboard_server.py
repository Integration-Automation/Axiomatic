"""Local axiomatic operations dashboard.

Run:
    py -3 axiomatic/dashboard_server.py

It serves a small HTML dashboard at http://127.0.0.1:8765/ and JSON at
/api/status. Stdlib-only, read-only, local bind by default.
"""
from __future__ import annotations

import ipaddress
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from _bot_config import load_bot_config
    from _process_control import _pid_alive
except ImportError:
    from axiomatic._bot_config import load_bot_config  # type: ignore
    from axiomatic._process_control import _pid_alive  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
DOROSSI_EVENTS_FILE = PROJECT_ROOT / "dorossi_events.ndjson"
DOROSSI_QUEUE_FILE = PROJECT_ROOT / "dorossi_queue.ndjson"
DOROSSI_FAILED_QUEUE_FILE = PROJECT_ROOT / "dorossi_queue_failed.ndjson"
GENERATE_HISTORY_FILE = PROJECT_ROOT / "generate_history.ndjson"
WEBRUNNER_LOG = PROJECT_ROOT / "webrunner.log"
WEBRUNNER_PID_FILE = PROJECT_ROOT / "webrunner.pid"
WEBRUNNER_PAUSE_FILE = PROJECT_ROOT / "webrunner.pause"
BATCH_LABEL_FILE = PROJECT_ROOT / "batch_label.txt"
OUTPUT_ROOT = PROJECT_ROOT / "output"


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
    分類與雙向對帳現在住在 `axiomatic/test_pid_file_readers.py`：掃到卻沒分類會紅，
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


def build_status() -> dict:
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
            "queue": _count_ndjson(DOROSSI_QUEUE_FILE),
            "failed_queue": _count_ndjson(DOROSSI_FAILED_QUEUE_FILE),
            "events": _read_ndjson_tail(DOROSSI_EVENTS_FILE, 10),
        },
        "generate": {
            "history": _read_ndjson_tail(GENERATE_HISTORY_FILE, 10),
        },
        "events": _read_ndjson_tail(EVENTS_FILE, 10),
        "output_images": _dir_image_count(OUTPUT_ROOT),
    }


HTML = """<!doctype html>
<html lang="zh-Hant">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>axiomatic Dashboard</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#101418;color:#e8edf2}
main{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:24px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{border:1px solid #2c3440;border-radius:8px;padding:14px;background:#171d24}
.k{color:#9fb0c0}.v{font-size:22px;font-weight:700}
pre{white-space:pre-wrap;word-break:break-word;background:#0b0f13;border-radius:8px;padding:12px;max-height:360px;overflow:auto}
button{background:#2f81f7;color:white;border:0;border-radius:6px;padding:8px 12px;font-weight:600}
</style>
<main>
<h1>axiomatic Dashboard</h1>
<button onclick="load()">Refresh</button>
<div id="cards" class="grid"></div>
<h2>Raw Status</h2>
<pre id="raw">loading...</pre>
</main>
<script>
function card(k, v){
  const box=document.createElement('div'); box.className='card';
  const kd=document.createElement('div'); kd.className='k'; kd.textContent=k;
  const vd=document.createElement('div'); vd.className='v'; vd.textContent=v;
  box.append(kd, vd); return box;
}
async function load(){
  const r=await fetch('/api/status',{cache:'no-store'});
  const s=await r.json();
  const cards=document.getElementById('cards');
  cards.replaceChildren(...[
    ['Background', s.webrunner.alive === null ? 'unknown (pid unreadable)' : (s.webrunner.alive ? 'running' : 'stopped')],
    ['Paused', s.webrunner.paused ? 'yes' : 'no'],
    ['Batch', s.label || '(none)'],
    ['Dorossi Queue', s.dorossi.queue],
    ['Failed Restore', s.dorossi.failed_queue],
    ['Output Images', s.output_images],
  ].map(([k,v])=>card(k,v)));
  document.getElementById('raw').textContent=JSON.stringify(s,null,2);
}
load(); setInterval(load, 15000);
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
                data = json.dumps(
                    build_status(), ensure_ascii=False).encode("utf-8")
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
