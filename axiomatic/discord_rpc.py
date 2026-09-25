r"""discord_rpc.py — 自製的 Discord 本機 Rich Presence client（等同 pypresence
的最小實作，純 stdlib、零新相依）。

跟 bot 那條「把目標使用者鏡像到 bot 自己 presence」是**完全不同的通道**：
這裡走 Discord **桌面 client** 開的本機 IPC（Windows named pipe
`\\.\pipe\discord-ipc-N`、Unix domain socket `$XDG_RUNTIME_DIR/discord-ipc-N`），
照官方 RPC 協定呼叫 `SET_ACTIVITY`，把活動卡片（details / state / 大小圖示
/ 時間軸 / 按鈕）寫到「目前登入這台機器 Discord 的使用者本人」身上。這是
遊戲顯示「Playing X」用的同一套正規介面，不是 self-bot、不碰 user token。

界線（平台限制，不是程式做不做得到）：
  * 只能寫 **Rich Presence Activity**；改不了使用者的 Custom Status 泡泡
    文字 / emoji，也改不了 online/idle/勿擾/隱身 的狀態燈。
  * 那行粗體應用名稱 = 你在 Discord Developer Portal 註冊的 application 名
    字（綁在 client_id 上）。圖片要先在該 app 的 "Art Assets" 上傳，設定檔
    用 asset key 引用。
  * 需要 Discord **桌面 app** 開著且登入；沒開 → connect 失敗、本模組
    安靜 no-op，下次有變化再重試。

設定檔：`presence_rpc.json`（repo 根目錄，optional）。schema 見
`_DEFAULT_RPC_CONFIG` 與 README。`enabled=false` 或沒填 `client_id` → 整段
停用。本模組與 `presence_probe.py` **共讀**同一個 JSON 但彼此不互相 import：
discord_rpc 讀 client_id / kinds 等「怎麼顯示」，presence_probe 讀 `claude`
區塊「要不要偵測 Claude」。

公開介面：
    load_rpc_config()                       → 完整 config dict（每 tick 讀，便宜）
    build_activity(probe, cfg, started_ms)  → SET_ACTIVITY 用的 activity dict / None
    class RichPresenceClient                → 連線 + 送 activity（blocking，
                                              呼叫端用 asyncio.to_thread 包）
"""
from __future__ import annotations

import copy
import json
import os
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RPC_CONFIG_FILE = PROJECT_ROOT / "presence_rpc.json"

# ---- IPC opcode（<op:uint32 LE><len:uint32 LE><utf-8 json payload>）--------
_OP_HANDSHAKE = 0
_OP_FRAME = 1
_OP_CLOSE = 2
_OP_PING = 3
_OP_PONG = 4

# Discord 對 details / state / *_text 的長度上限是 128。
_MAX_LEN = 128

# 對方宣告的 frame 長度上限。正常回應只有幾百 bytes；pipe 損毀 / 之前寫入
# 沒對齊導致 frame 錯位時，長度欄位會被讀成垃圾（最大可到 4 GiB），
# 若照單全收就會在 worker thread 裡吃光記憶體。超過上限 → 視為連線壞掉，
# 斷線後下個 tick 重連。
_MAX_FRAME = 1 << 20        # 1 MiB

# Unix domain socket 的 I/O timeout。桌面 app 正常都是毫秒級回應，這裡只是
# 避免對方卡死時把呼叫端的 worker thread 永久掛住。
# 注意：Windows named pipe 走的是一般檔案物件，**沒有**對應的 timeout 機制，
# 那條路徑的讀取仍可能無限期阻塞（見模組說明）。
_IPC_TIMEOUT_SEC = 10.0


# ---------- 設定檔 ----------------------------------------------------------

# 每個 probe kind 的「怎麼顯示」預設。name / details / state / *_text 支援
# `{name}` 佔位符（會被偵測到的遊戲名 / 歌名 / "Claude Code" 取代）。
#   name        → 狀態最上面那行粗體字。**預設用執行中的應用程式名**
#                 （`{name}`），實測 Discord 桌面 client 的 RPC 會吃這個欄位
#                 （不再被鎖死成註冊的 application 名）。
#   large_image / small_image → 上傳到 app Art Assets 的 asset key。
#                 **預設留空 = 不顯示圖片**；要圖片再自己填 key（並上傳）。
#   show_timestamp=true → 帶 start 時間戳，Discord 端顯示「已經過 MM:SS」。
#   type：0=Playing、2=Listening、3=Watching。
_DEFAULT_KIND_CONFIG: dict = {
    "claude": {
        "type": 0,
        "name": "{name}",
        "details": "",
        "state": "",
        "large_image": "",
        "large_text": "",
        "small_image": "",
        "small_text": "",
        "show_timestamp": True,
    },
    "playing": {
        "type": 0,
        "name": "{name}",
        "details": "",
        "state": "",
        "large_image": "",
        "large_text": "",
        "small_image": "",
        "small_text": "",
        "show_timestamp": True,
    },
    "listening": {
        "type": 2,
        "name": "{name}",
        "details": "",
        "state": "",
        "large_image": "",
        "large_text": "",
        "small_image": "",
        "small_text": "",
        "show_timestamp": False,
    },
}

# float 轉得出來的最大值。`_is_finite_number` 用**比較**而不是 `math.isfinite()`，
# 因為 JSON 的整數沒有位數上限，而 `float(10**400)` 與 `math.isfinite(10**400)`
# 兩個都會丟 `OverflowError`——`load_rpc_config` 的合約是絕不往外拋。
_FLOAT_MAX = 1.7976931348623157e+308


def _is_finite_number(value) -> bool:
    """不是 bool、是 int/float、而且**轉得成有限的 float**。

    `_batch_config` / `_bot_config` 各有一份同名同義的實作，理由與這裡完全相同；
    這是第三份。**沒有共用是刻意的**：那兩支是被動共用模組，而 `discord_rpc` 是
    bot 專屬的 helper，為了一個五行的述詞去建立一條新的模組相依不划算。抄一份的
    代價由 `test_config_numbers` 的全專案掃描扛著——它現在會要求每一個「從設定檔
    收下數字再拿去比大小」的地方都問過有限性。

    2026-09-10 之前這裡是 `isinstance(rs, (int, float)) and not isinstance(rs,
    bool) and rs > 0`，而 **`inf > 0` 為真**。`1e400` 是完全正常的 JSON 字面值、
    parse 出來就是 `inf`（`nan` 反而擋得掉——所有比較對 nan 都是假），於是
    `refresh_sec` 可以變成 `inf`，而 `apply()` 的保活節流是
    `(now - self._last_send_ts) < refresh_sec`——**恆為真，保活從此再也不會送**。
    保活不是裝飾：它存在的理由就是「pipe 死掉 → 送失敗 → 斷線 → 下次自動重連」，
    也就是偵測桌面端已經關掉。而且完全無聲：`apply()` 回 `unchanged`，
    `discord_bot._rpc_health_class` 把它歸類成健康，所以連 log 節流那一層都不會
    出聲。實測一整天的 probe tick：正常設定送 4 次，`inf` 只送 1 次。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        # 一個範圍比較同時擋掉三種東西，不需要再寫 `value == value` 或
        # `math.isfinite()`：`nan` 的**所有**比較都是假，所以它過不了左邊那一半；
        # `inf` 過不了右邊；超大 int 用比較不會溢位。第一版多寫了一個
        # `value == value`，變異測試指出它是死碼（拿掉之後行為完全相同）——
        # 讀起來像承重、實際上不是的條件，比沒有註解更糟。
        return -_FLOAT_MAX <= value <= _FLOAT_MAX
    return False


_DEFAULT_RPC_CONFIG: dict = {
    "enabled": False,            # 沒設好之前一律停用，避免噴錯
    "client_id": "",             # Developer Portal 的 Application ID
    "refresh_sec": 60.0,         # 內容沒變時，每隔多久仍重送一次（偵測斷線 + 保活）
    # bot 的遠端鏡像（on_presence_update）預設略過「我們自己用 RPC 灌到使用
    # 者帳號上的那個 activity」（用 application_id == client_id 判斷），避免
    # 鏡像鏡到自己；設 false 則照舊把它也鏡進來。
    "mirror_ignore_own_rpc": True,
    "kinds": _DEFAULT_KIND_CONFIG,
    # `claude` 區塊由 presence_probe.py 讀（偵測用），這裡不碰，但保留在
    # 預設裡讓 load_rpc_config 的回傳完整、方便 /probe_status 一起印。
    "claude": {"enabled": True, "process_names": ["claude.exe"]},
}


def _merge_kind(raw, default: dict) -> dict:
    """單一 kind 的設定：raw 覆蓋 default，型別不符就退回 default 那欄。"""
    out = dict(default)
    if not isinstance(raw, dict):
        return out
    for key in ("name", "details", "state", "large_image", "large_text",
                "small_image", "small_text"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    if isinstance(raw.get("show_timestamp"), bool):
        out["show_timestamp"] = raw["show_timestamp"]
    if isinstance(raw.get("type"), int) and not isinstance(raw.get("type"), bool):
        out["type"] = raw["type"]
    return out


def load_rpc_config() -> dict:
    """讀 `presence_rpc.json`，缺檔 / 壞檔 / 型別不符一律退回預設、不丟例外。
    每次呼叫都重讀（跟 presence_games.json 一致），讓編輯設定檔下個 probe
    tick 就生效，不必 !restart。"""
    cfg = {
        "enabled": _DEFAULT_RPC_CONFIG["enabled"],
        "client_id": _DEFAULT_RPC_CONFIG["client_id"],
        "refresh_sec": _DEFAULT_RPC_CONFIG["refresh_sec"],
        "mirror_ignore_own_rpc": _DEFAULT_RPC_CONFIG["mirror_ignore_own_rpc"],
        # **`deepcopy` 不是潔癖。** 淺複製只會複製外層，`claude.process_names`
        # 那個 list 仍與模組常數是同一個物件——呼叫端 append 一次，預設值就被
        # 永久污染，而這支每個 probe tick 都會被叫一次，所以髒的會一路傳下去。
        # `_bot_config._fallback_bot_config` 為了同一件事踩過同一個坑（那次是
        # `user_roles` 的三份 id 清單，污染它等於讓權限閘門憑空變成「已設定」）。
        # 對照：`presence_probe._load_claude_detection` 讀同一個區塊卻可以共用
        # 同一個物件，因為它回的是 tuple——差別只在容器可不可變。
        "kinds": copy.deepcopy(_DEFAULT_KIND_CONFIG),
        "claude": copy.deepcopy(_DEFAULT_RPC_CONFIG["claude"]),
    }
    try:
        text = RPC_CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return cfg
        # `UnicodeDecodeError` 是 `ValueError` 的子類別、**不是** `OSError`：
        # 一個被別的編輯器另存成 Big5 的設定檔就會從這裡逸出，而本機 locale
        # 正是 cp950、檔案內容幾乎都含中文。不用 `errors="replace"` 靜靜吞掉——
        # 那會把亂碼當成有效值用下去，比退回預設糟。
    except (OSError, UnicodeDecodeError) as error:
        print(f"discord_rpc: read {RPC_CONFIG_FILE.name} failed: {error!r}",
              file=sys.stderr)
        return cfg
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError as error:
        print(f"discord_rpc: parse {RPC_CONFIG_FILE.name} failed: {error!r}",
              file=sys.stderr)
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get("enabled"), bool):
        cfg["enabled"] = data["enabled"]
    if isinstance(data.get("client_id"), (str, int)):
        cfg["client_id"] = str(data["client_id"]).strip()
    rs = data.get("refresh_sec")
    if _is_finite_number(rs) and rs > 0:
        cfg["refresh_sec"] = float(rs)
    if isinstance(data.get("mirror_ignore_own_rpc"), bool):
        cfg["mirror_ignore_own_rpc"] = data["mirror_ignore_own_rpc"]
    kinds = data.get("kinds")
    if isinstance(kinds, dict):
        for kind, default in _DEFAULT_KIND_CONFIG.items():
            cfg["kinds"][kind] = _merge_kind(kinds.get(kind), default)
    claude = data.get("claude")
    if isinstance(claude, dict):
        if isinstance(claude.get("enabled"), bool):
            cfg["claude"]["enabled"] = claude["enabled"]
        pn = claude.get("process_names")
        if isinstance(pn, list):
            names = [s for s in pn if isinstance(s, str) and s.strip()]
            if names:
                cfg["claude"]["process_names"] = names
    return cfg


def build_activity(probe, cfg: dict, started_at_ms: int) -> dict | None:
    """把 probe 結果（`{"kind","name"}` 或 None）+ presence_rpc.json 的
    kinds 設定組成 SET_ACTIVITY 的 activity dict。probe=None → 回 None
    （清掉狀態）。空欄位一律省略，避免送出 Discord 不接受的空字串。"""
    if not probe:
        return None
    kind = probe.get("kind")
    name = (probe.get("name") or "").strip()
    kind_cfg = cfg.get("kinds", {}).get(kind)
    if not kind_cfg:
        return None

    def _fmt(value: str) -> str:
        # The template is a hand-written config value and can be mistyped in more
        # than one way: `{nam}` is KeyError, `{0}` IndexError, `{name:>5x}`
        # ValueError, `{name.foo}` AttributeError and `{name[x]}` TypeError. The last
        # two used to escape here on every heartbeat, freezing the status on the last
        # good one. A mistyped template is sent as written instead.
        try:
            return value.format(name=name)
        except (KeyError, IndexError, ValueError, AttributeError, TypeError):
            return value

    activity: dict = {}
    # name = 最上面那行粗體字。預設 `{name}` → 執行中的應用程式名（Discord
    # RPC 實測會吃這個欄位）。
    act_name = _fmt(kind_cfg.get("name", "")).strip()
    if act_name:
        activity["name"] = act_name[:_MAX_LEN]
    details = _fmt(kind_cfg.get("details", "")).strip()
    state = _fmt(kind_cfg.get("state", "")).strip()
    if details:
        activity["details"] = details[:_MAX_LEN]
    if state:
        activity["state"] = state[:_MAX_LEN]

    assets: dict = {}
    large_image = (kind_cfg.get("large_image") or "").strip()
    large_text = _fmt(kind_cfg.get("large_text", "")).strip()
    small_image = (kind_cfg.get("small_image") or "").strip()
    small_text = _fmt(kind_cfg.get("small_text", "")).strip()
    if large_image:
        assets["large_image"] = large_image
    if large_text:
        assets["large_text"] = large_text[:_MAX_LEN]
    if small_image:
        assets["small_image"] = small_image
    if small_text:
        assets["small_text"] = small_text[:_MAX_LEN]
    if assets:
        activity["assets"] = assets

    if kind_cfg.get("show_timestamp"):
        activity["timestamps"] = {"start": int(started_at_ms)}

    a_type = kind_cfg.get("type")
    if isinstance(a_type, int) and not isinstance(a_type, bool):
        activity["type"] = a_type

    # Discord 要求 activity 至少帶一個可顯示欄位；name / details / state 全空
    # 才用偵測名稱補 name，免得送出一個空殼被拒。
    if not activity.get("name") and not activity.get("details") \
            and not activity.get("state"):
        activity["name"] = (name or "Active")[:_MAX_LEN]
    return activity


# ---------- transport（Windows named pipe / Unix domain socket）------------

class _Transport:
    """把 Windows named pipe（檔案物件）跟 Unix domain socket 包成同一組
    read/write/close 介面。Windows 上 named pipe 直接用 open(path,'r+b') 當
    binary 檔讀寫即可。"""

    def __init__(self, file_obj=None, sock=None, path=""):
        self._file = file_obj
        self._sock = sock
        self.path = path

    def write(self, data: bytes) -> None:
        if self._file is not None:
            # buffering=0 → FileIO（raw）。RawIOBase.write 依契約允許「短寫」
            # 並回傳實際寫入量（非阻塞時甚至回 None），不能假設一次寫完：
            # 少寫幾個 byte 會讓後續 frame 整個錯位，接著就是讀到垃圾長度。
            view = memoryview(data)
            while view:
                written = self._file.write(view)
                if not written:
                    raise OSError("IPC write made no progress")
                view = view[written:]
            self._file.flush()
        else:
            self._sock.sendall(data)

    def read(self, n: int) -> bytes:
        if self._file is not None:
            return self._file.read(n)
        return self._sock.recv(n)

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
            elif self._sock is not None:
                self._sock.close()
        except OSError:
            pass


def _running_on_windows() -> bool:
    """這個行程是不是跑在 Windows 上。

    **抽成具名函式是為了讓測試有一個接得上的接縫，這不是多餘的包裝。**
    2026-09-12 真的踩過：`_open_transport` 的述詞從 `sys.platform == "win32"` 改成
    `os.name == "nt"`，但四支模擬 POSIX 的測試仍然在 `monkeypatch.setattr(sys,
    "platform", "linux")`——**替身換掉的接縫已經不是正式碼在讀的那一個**。於是
    Windows 具名管道那條分支照跑，連上真的桌面程式、回傳一個真的 transport。
    更糟的是它**會看人臉色**：桌面程式沒開的時候四支全綠，所以那天之前它一直是綠的。

    改成一個函式之後，「正式碼讀的」與「測試換的」在結構上就是同一個東西，
    再怎麼改拼法也不會各走各的。測試請 patch 這一支，**不要** patch `os.name` 或
    `sys.platform`——那兩個是全域，行程裡還有別的東西在讀。
    """
    return os.name == "nt"


def _open_transport() -> _Transport | None:
    """掃 discord-ipc-0 ~ 9，第一個連得上的就回 _Transport；全失敗回 None
    （多半是 Discord 桌面 app 沒開）。"""
    if _running_on_windows():
        for i in range(10):
            path = rf"\\.\pipe\discord-ipc-{i}"
            try:
                f = open(path, "r+b", buffering=0)
            except OSError:
                continue
            return _Transport(file_obj=f, path=path)
        return None
    # Unix：socket 檔放在 runtime dir（含 Flatpak / snap 常見子目錄）。
    base = (os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("TMPDIR")
            or os.environ.get("TMP")
            or os.environ.get("TEMP")
            or "/tmp")
    subdirs = ("", "app/com.discordapp.Discord",
               "app/com.discordapp.DiscordCanary", "snap.discord")
    for sub in subdirs:
        folder = os.path.join(base, sub) if sub else base
        for i in range(10):
            path = os.path.join(folder, f"discord-ipc-{i}")
            if not os.path.exists(path):
                continue
            s = None
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(_IPC_TIMEOUT_SEC)
                s.connect(path)
            except OSError:
                # 連不上就把 socket 關掉再試下一個；只靠 refcount 回收會留下
                # ResourceWarning，掃 10 個路徑 × 每個 tick 會累積 fd 壓力。
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
                continue
            return _Transport(sock=s, path=path)
    return None


class RichPresenceClient:
    """管一條到 Discord 桌面 client 的 IPC 連線。所有方法都是 **blocking**
    （named pipe / socket 同步 I/O），呼叫端（bot 的 async probe loop）要用
    `asyncio.to_thread` 包起來，別在 event loop 直接呼叫。

    單一 probe loop 串行呼叫，內部不另做鎖。"""

    def __init__(self) -> None:
        self._t: _Transport | None = None
        self._client_id: str = ""
        self._connected: bool = False
        self._last_payload_key: str | None = None
        self._last_send_ts: float = 0.0
        self._last_error: str = ""

    # ---- 低階收送 ----
    def _send(self, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._t.write(struct.pack("<II", op, len(data)) + data)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._t.read(n - len(buf))
            if not chunk:
                raise OSError("IPC pipe closed (EOF)")
            buf += chunk
        return bytes(buf)

    def _recv(self, _depth: int = 0) -> tuple[int, dict]:
        op, length = struct.unpack("<II", self._read_exact(8))
        if length > _MAX_FRAME:
            # 長度欄位明顯是垃圾（frame 錯位 / pipe 損毀）。照著讀下去等於
            # 在 worker thread 裡試著配置最多 4 GiB。當成連線壞掉處理。
            raise OSError("IPC frame too large")
        raw = self._read_exact(length) if length else b""
        if op == _OP_PING and _depth < 4:
            # 回 PONG 後繼續讀我們真正要等的那個 frame。
            try:
                self._send(_OP_PONG, json.loads(raw or b"{}"))
            except (OSError, ValueError):
                # ValueError 涵蓋 JSONDecodeError 與 bytes 解碼失敗的
                # UnicodeDecodeError（後者不是 JSONDecodeError，原本會漏接）。
                pass
            return self._recv(_depth + 1)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        return op, parsed if isinstance(parsed, dict) else {}

    # ---- 連線生命週期 ----
    def _fail(self, detail: str) -> None:
        """記錄一次失敗細節。細節只進 stderr / `status()["last_error"]`，
        **絕不能**被 `apply()` 當回傳值帶出去 —— 呼叫端會把回傳值原樣貼進
        聊天訊息（狀態指令），而原始例外文字 / 路徑 / 服務名不得外流。
        同一個錯誤連續發生時只印第一次，避免對方沒開時每個 tick 洗一行。"""
        if detail != self._last_error:
            print(f"discord_rpc: {detail}", file=sys.stderr)
        self._last_error = detail

    def _disconnect(self) -> None:
        if self._t is not None:
            self._t.close()
        self._t = None
        self._connected = False

    def _connect(self, client_id: str) -> bool:
        self._disconnect()
        transport = _open_transport()
        if transport is None:
            self._fail("IPC pipe 找不到（桌面 app 沒開？）")
            return False
        self._t = transport
        try:
            self._send(_OP_HANDSHAKE, {"v": 1, "client_id": str(client_id)})
            op, data = self._recv()
        except OSError as error:
            self._fail(f"handshake I/O 失敗: {error!r}")
            self._disconnect()
            return False
        if op == _OP_CLOSE:
            self._fail(f"handshake 被拒（client_id 無效？）: {data}")
            self._disconnect()
            return False
        self._client_id = str(client_id)
        self._connected = True
        # 強制下一次真的送。用 -inf 而不是 0.0：節流基準已改成 monotonic，
        # 剛開機時 monotonic() 可能小於 refresh_sec，0.0 就不再保證「過期」。
        self._last_send_ts = float("-inf")
        self._last_payload_key = None
        self._last_error = ""
        return True

    def close(self) -> None:
        """主動清掉 presence 並關連線（盡力而為）。"""
        if self._connected and self._t is not None:
            try:
                self._send(_OP_FRAME, {
                    "cmd": "SET_ACTIVITY",
                    "args": {"pid": os.getpid(), "activity": None},
                    "nonce": str(uuid.uuid4()),
                })
            except OSError:
                pass
        self._disconnect()

    # ---- 對外主入口 ----
    def apply(self, activity: dict | None, client_id: str,
              refresh_sec: float = 60.0) -> str:
        """設定（或以 activity=None 清掉）使用者的 Rich Presence。

        回傳值是**固定詞彙**的其中一個：`disabled` / `not-connected` /
        `unchanged` / `ok` / `send-failed` / `rejected`。呼叫端會把它原樣貼進
        聊天訊息（狀態指令），所以這裡**不可以**把失敗細節串進回傳值 —— 原始
        例外文字、pipe 路徑、服務名一律只進 stderr 與 `status()["last_error"]`
        （呼叫端已知那欄要轉 log，不會外送）。

        去重 + 保活都在這裡：內容沒變且距上次送出 < refresh_sec → 直接回
        unchanged 不做 I/O；超過 refresh_sec 仍重送一次，順便偵測桌面 app
        是否已經關掉（pipe 死掉 → 送失敗 → 斷線，下次自動重連）。"""
        if not client_id:
            if self._connected:
                self._disconnect()
            return "disabled"
        if (not self._connected) or (self._client_id != str(client_id)):
            if not self._connect(client_id):
                return "not-connected"

        key = (json.dumps(activity, sort_keys=True, ensure_ascii=False)
               if activity is not None else "null")
        # 保活節流用 monotonic：wall clock 被 NTP / 手動調回去時，
        # `now - _last_send_ts` 會變負數，於是永遠判定「還沒到 refresh_sec」，
        # presence 就卡在最後一次的內容直到時鐘追回來。
        now = time.monotonic()
        if key == self._last_payload_key and (now - self._last_send_ts) < refresh_sec:
            return "unchanged"

        try:
            self._send(_OP_FRAME, {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": str(uuid.uuid4()),
            })
            op, data = self._recv()
        except OSError as error:
            self._fail(f"送出失敗: {error!r}")
            self._disconnect()
            return "send-failed"
        if op == _OP_CLOSE:
            # 對方在收下 frame 後把連線收掉了。不斷線的話 _connected 會停在
            # True，接下來每次都以為送成功（還把 payload key 記起來去重），
            # presence 實際上沒套用卻無聲無息。
            self._fail("連線在送出後被對方關閉")
            self._disconnect()
            return "send-failed"
        if data.get("evt") == "ERROR":
            # 被拒的 activity 是走**正常 FRAME** 回來的（`{"evt": "ERROR",
            # "data": {"code":…, "message":…}}`），不是 CLOSE。只看 op 的話這裡
            # 會一路走到 `return "ok"`，還把 payload key 記進去重快取——於是接
            # 下來整個 refresh_sec 都不再重送，狀態指令也顯示 ok，實際上狀態從
            # 頭到尾沒設起來。連線本身是好的（欄位無效 / asset key 不存在之類），
            # 所以**不斷線**：保持連線、不記 payload key，下個 tick 自然重試。
            detail = data.get("data")
            if isinstance(detail, dict):
                code = detail.get("code")
                message = detail.get("message")
            else:
                code = message = None
            self._fail(f"activity 被拒 (code={code!r}): {message!r}")
            return "rejected"
        self._last_payload_key = key
        self._last_send_ts = now
        # **成功一次就把上一次的失敗細節清掉。** 原本只有 `_connect()` 成功時會清，
        # 而「activity 被拒」那條路**刻意不斷線**（連線本身是好的），所以它記下的錯誤
        # 永遠等不到 `_connect()`，會一直留著。兩層後果，第二層才是真的痛：
        #   1. 狀態指令一直報一個早就過去的失敗，看起來像 RPC 還壞著；
        #   2. `_fail()` 的去重比的是「上一次記到的字串」而不是「上一個 tick」，
        #      所以**同一個錯誤在夾了一次成功之後再犯，就不會再寫進 stderr**——
        #      log 只留得下第一次。也就是說**會重複發生的問題，正好是最看不見的那種**。
        # 清在這裡不會影響「桌面程式沒開」那條真正吵的去重：那條走的是
        # `not-connected`，永遠到不了這一行。
        self._last_error = ""
        return "ok"

    def status(self) -> dict:
        """連線診斷快照。

        呼叫端注意：`pipe`（本機路徑）與 `last_error`（含原始例外文字）
        **只能進 log**，不得放進送往聊天平台的訊息。"""
        # 這是唯一會跟 apply() 併行執行的方法（apply 在 worker thread，
        # 狀態指令在 event loop 直接呼叫）。self._t 要先抓成區域變數：
        # 直接寫 `self._t.path if self._t is not None else ""` 的話，
        # 檢查與取值之間若剛好被 _disconnect() 插進來就會 AttributeError。
        transport = self._t
        return {
            "connected": self._connected,
            "client_id": self._client_id,
            "pipe": transport.path if transport is not None else "",
            "last_error": self._last_error,
        }
