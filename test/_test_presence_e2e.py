"""End-to-end test: 不開 Discord client，直接驗證 probe → change_presence
的整條 wiring。Patch 三個東西：
  1. client.change_presence → 記錄被呼叫了什麼
  2. presence_probe.probe_smtc_raw_async → 回傳假的 Spotify session
  3. presence_probe._load_game_whitelist → 回傳一筆假遊戲

跑三輪 probe，第一輪假的 Spotify、第二輪假的遊戲、第三輪都沒有，看 bot 有沒有
真的去更新狀態。

    py -3 test/_test_presence_e2e.py

**為什麼用 `_test_` 前綴讓 pytest 不收集。** 它把模組層的全域**改掉就不還原**
（`client.change_presence`、`presence_probe` 的三個函式、`psutil.process_iter`），
在自己的行程裡跑完就結束沒關係，被 pytest 收進同一個直譯器就會污染後面的測試。
要納入自動化的話得整支改寫成 `monkeypatch`，而它存在的價值本來就在「跑一次真的
`probe_signals_async`、看它在這台機器上實際挑出什麼」，那件事在 CI 式的執行裡沒有
意義（本專案也沒有 CI）。

**它守的那一段從 2026-09-20 起另外有 pytest 守門。** `_apply_combined_presence`
（去重快取、三條 except、來源標籤）在那之前覆蓋率是 0，現在由
`test_bot_presence_logging.py` 第四組涵蓋；這支保留為手動的整條鏈檢查。
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))
import discord  # noqa: E402
import presence_probe  # noqa: E402
import discord_bot  # noqa: E402


CALLS: list[tuple] = []


async def fake_change_presence(*, status=None, activity=None, **kw):
    desc = "None"
    if isinstance(activity, discord.Spotify):
        desc = f"Spotify({activity.title})"
    elif isinstance(activity, discord.Game):
        desc = f"Game({activity.name!r})"
    elif isinstance(activity, discord.Activity):
        desc = f"Activity(type={activity.type}, name={activity.name!r})"
    elif isinstance(activity, discord.Streaming):
        desc = f"Streaming({activity.name!r})"
    CALLS.append((str(status), desc))
    print(f"  change_presence called: status={status} activity={desc}")


async def _run_bot_probe():
    """跑一次 production 用的 bot 鏡像路徑：probe_signals_async →
    bot_activity_from_signals → 套用，跟 `_presence_probe_loop` 一致。"""
    signals = await presence_probe.probe_signals_async()
    bot_probe = presence_probe.bot_activity_from_signals(signals)
    print(f"  bot probe result: {bot_probe}")
    if bot_probe is None:
        discord_bot._local_probed_activity = None
    elif bot_probe["kind"] == "listening":
        discord_bot._local_probed_activity = discord.Activity(
            type=discord.ActivityType.listening, name=bot_probe["name"])
    else:
        discord_bot._local_probed_activity = discord.Game(name=bot_probe["name"])
    await discord_bot._apply_combined_presence()


async def main() -> int:
    print("--- test 1: 假裝 Spotify 在播 ---")
    discord_bot.client.change_presence = fake_change_presence  # type: ignore
    # 這個 e2e 測 Spotify → Game → None 三段，Claude 偵測會污染 test 3
    #（claude.exe 真的在跑），所以整段把它 mock 掉。
    presence_probe.probe_claude_code = lambda: None  # type: ignore

    async def fake_smtc_spotify(timeout=6.0):
        return {"title": "Bohemian Rhapsody", "artist": "Queen",
                "source": "Spotify.exe"}

    presence_probe.probe_smtc_raw_async = fake_smtc_spotify  # type: ignore
    presence_probe._load_game_whitelist = lambda: {}  # type: ignore

    # 不跑無窮迴圈，只手動跑一次 probe + apply
    await _run_bot_probe()

    print()
    print("--- test 2: 假裝 endfield.exe 在跑 ---")

    async def fake_smtc_none(timeout=6.0):
        return None

    presence_probe.probe_smtc_raw_async = fake_smtc_none  # type: ignore
    presence_probe._load_game_whitelist = lambda: {  # type: ignore
        "endfield.exe": "Arknights: Endfield",
    }

    import psutil
    real_iter = psutil.process_iter

    def fake_iter(attrs=None):
        class P:
            def __init__(self, name):
                self.info = {"name": name}
        yield P("endfield.exe")
        yield P("chrome.exe")
    psutil.process_iter = fake_iter  # type: ignore

    await _run_bot_probe()
    psutil.process_iter = real_iter  # type: ignore

    print()
    print("--- test 3: 都沒有，期望清回 None ---")

    async def fake_smtc_again_none(timeout=6.0):
        return None
    presence_probe.probe_smtc_raw_async = fake_smtc_again_none  # type: ignore
    presence_probe._load_game_whitelist = lambda: {}  # type: ignore

    await _run_bot_probe()

    print()
    print(f"=== change_presence 共被呼叫 {len(CALLS)} 次 ===")
    for i, c in enumerate(CALLS, 1):
        print(f"  [{i}] {c}")
    expected = 3
    if len(CALLS) == expected:
        print("OK: 3 次呼叫符合預期（Spotify → Game → None）")
        return 0
    print(f"FAIL: 預期 {expected} 次但只看到 {len(CALLS)} 次")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
