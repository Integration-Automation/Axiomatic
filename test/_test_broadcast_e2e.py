"""手動 e2e：從**閘道那一側**看目標使用者身上到底掛了幾張 activity。

跟 `_test_presence_e2e.py` 是不同的東西，兩支都要留：
- `_test_presence_e2e.py` 驗**本機這一側**的接線（probe → change_presence），
  全部 patch 掉、不連網。
- 這一支開一條**真的唯讀閘道連線**，回答「送出去的到底有沒有廣播出去」。
  那是本機怎麼看都看不到的——活動隱私開關關著時 `SET_ACTIVITY` **照收、照回
  成功**（`apply()` 回 `ok`、連圖片 key 都會被解析成真實 id），只是不對外廣播。

`_test_` 前綴讓 pytest 不收集：它要憑證、要網路、要該 application 開著
presence／members 兩個特權 intent，不屬於單元測試。

    py -3 test/_test_broadcast_e2e.py

輸出的三種結論，對應三種完全不同的處置：
- **0 張** → 收下了卻沒廣播，去開桌面 app 的活動隱私開關。
- **有我們那張、排第一** → 一切正常。
- **有我們那張、排第 N** → 廣播沒問題，是平台自己的偵測器（多半是遊戲偵測）
  先佔住顯示槽。我們的優先序只決定「送哪一張」，管不到平台自己送的那張；要讓
  我們的卡片遞補，得把桌面 app 對該程式的偵測關掉。

**刻意不給 `Client()` 傳 `activity` / `status`**：那樣 IDENTIFY 就不含 presence
欄位，這條臨時連線不會動到正在跑的 bot 自己的狀態。實測（2026-08-23）跑完之後
bot 行程沒有重啟、狀態沒變。只讀不寫，讀完立刻 close。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import discord  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CONNECT_TIMEOUT_SEC = 90


def _load() -> tuple[str, str, int]:
    token = (REPO_ROOT / "discord_bot_token.md").read_text(
        encoding="utf-8").strip().splitlines()[0].strip()
    cfg = json.loads((REPO_ROOT / "bot_config.json").read_text(encoding="utf-8"))
    rpc = json.loads((REPO_ROOT / "presence_rpc.json").read_text(encoding="utf-8"))
    target = (cfg.get("target_presence_username") or "").lower()
    app_id = int((str(rpc.get("client_id") or "")).strip() or 0)
    return token, target, app_id


def _activity_type(act) -> str:
    kind = getattr(act, "type", None)
    return getattr(kind, "name", str(kind))


def _report(member, app_id: int) -> None:
    print(f"status        : {member.status}")
    print(f"  desktop={member.desktop_status} mobile={member.mobile_status} "
          f"web={member.web_status}")
    # 一次掃完就好——index 與 total 必須來自同一份快照，分兩次讀可能對不起來
    # （跟 `discord_bot._rpc_broadcast_state` 同樣的理由）。
    acts = list(getattr(member, "activities", ()) or ())
    print(f"activities    : {len(acts)}")
    ours = None
    for idx, act in enumerate(acts):
        # `discord.Game` 沒有 application_id，所以用 getattr 而不是直接取。
        owner = getattr(act, "application_id", None)
        mark = ""
        if owner == app_id:
            ours = idx
            mark = "   <== 我們送的那張"
        print(f"  [{idx}] type={_activity_type(act):<12} "
              f"name={getattr(act, 'name', None)!r:<28} "
              f"application_id={owner}{mark}")
    print()
    if not acts:
        print("VERDICT: 收下了卻沒有廣播 —— 一張都沒有（活動隱私開關）")
    elif ours is None:
        print(f"VERDICT: 有 {len(acts)} 張，但沒有一張是我們送的"
              f"（application_id != {app_id}）")
    elif ours == 0:
        print(f"VERDICT: 有廣播，而且排第一張（共 {len(acts)} 張）")
    else:
        print(f"VERDICT: 有廣播，但排第 {ours + 1} 張 / 共 {len(acts)} 張 —— "
              f"前面的多半是平台自己的偵測器")


async def main() -> int:
    token, target, app_id = _load()
    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    client = discord.Client(intents=intents)   # 不給 activity/status

    @client.event
    async def on_ready():  # pylint: disable=unused-variable
        try:
            print(f"connected. guilds={len(client.guilds)}")
            member = None
            for guild in client.guilds:
                for candidate in guild.members:
                    name = (getattr(candidate, "name", "") or "").lower().lstrip(".")
                    if name == target:
                        member = candidate
                        break
                if member is not None:
                    print(f"target found in guild {guild.name!r} "
                          f"(members cached={len(guild.members)})")
                    break
            if member is None:
                print("TARGET NOT FOUND —— 不在任何共同伺服器的成員快取裡")
                return
            _report(member, app_id)
        finally:
            await client.close()

    try:
        await asyncio.wait_for(client.start(token), timeout=CONNECT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        print(f"TIMEOUT: on_ready 沒有在 {CONNECT_TIMEOUT_SEC} 秒內完成")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("PrivilegedIntentsRequired: 該 application 沒開 presence / members "
              "特權 intent")
        return 1
    finally:
        if not client.is_closed():
            await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
