"""`/proc usage`：角色分類、合計、降級，以及送出面的擁有者閘。

**整份測試一個真行程都不碰。** 行程表是這台機器當下的狀態——批次在不在跑、有沒有
開瀏覽器、別人的編輯器叫什麼名字，每一項都會讓斷言在別台機器上得到不同答案。那種
測試的結局永遠是被標成 skip，然後這一整條路就沒有守門了。所以行程來源一律注入
（`_resource_report.collect(scan=…, machine=…)`），真機上的數字只在改這條路的時候
手動量一次，量測結果寫進 `architecture.md`。

守住的東西分三層：

1. **分類**——誰算 bot、誰算批次、誰算瀏覽器、誰算對話助理的後端。判錯的代價不對
   稱：多算會把使用者自己的工具說成本專案的負擔，少算會讓「對話助理的後端」永遠
   是 0，而 0 跟「現在沒在跑」長得一模一樣。
2. **合計**——讀不到的欄位不得用 0 充數（0 會被加進總和，然後那個總和會被當成事實
   講出去），轉接殼只對 Python 那三個角色併。
3. **送出面**——PID 與主機路徑只給擁有者（秘密分層 1），其餘人拿泛用標籤。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _resource_report as rr                       # noqa: E402


# --------------------------------------------------------------------------
# 假的行程表
# --------------------------------------------------------------------------
def row(pid, name="python.exe", *, ppid=None, script=None, started=1000.0,
        rss=1024, cpu=0.0):
    return rr.ProcRow(pid=pid, name=name, ppid=ppid, script=script,
                      started=started, rss=rss, cpu=cpu)


def scan_of(rows, *, scan_ok=True, notes=(), system_cpu=5.0,
            descendant_roots=None):
    result = rr.ScanResult(rows=tuple(rows), scan_ok=scan_ok, notes=tuple(notes),
                           system_cpu=system_cpu,
                           descendant_roots=dict(descendant_roots or {}))
    return lambda: result


def machine_of(facts=None, notes=()):
    base = {"cpu_count": 4, "mem_total": 8 * 1024 ** 3,
            "mem_available": 2 * 1024 ** 3, "disk_total": 500 * 1024 ** 3,
            "disk_free": 100 * 1024 ** 3, "disk_path": r"D:\somewhere\output"}
    base.update(facts or {})
    return lambda: (base, list(notes))


# --------------------------------------------------------------------------
# 1. 分類
# --------------------------------------------------------------------------
@pytest.mark.parametrize("script,expected", [
    ("discord_bot.py", rr.ROLE_BOT),
    ("start_discord_bot.py", rr.ROLE_LAUNCHER),
    ("start_webrunner.py", rr.ROLE_LAUNCHER),
    ("run_batch.py", rr.ROLE_LAUNCHER),
    ("webrunner_novelai.py", rr.ROLE_BATCH),
    ("webrunner_je_only.py", rr.ROLE_BATCH),
])
def test_a_python_process_is_classified_by_the_script_it_runs(script, expected):
    assert rr.role_of_row(row(1, script=script)) == expected


@pytest.mark.parametrize("name", ["chrome.exe", "CHROME.EXE", "chromedriver.exe"])
def test_every_browser_process_counts_regardless_of_case(name):
    """判準刻意與啟動前的清理流程一致（`_find_all_chrome_processes` 無條件掃全部）。

    不一致的後果不是數字難看，是**兩邊各說各話**：清單說 24 個、下一次清理殺掉 30
    個，而使用者是照著這份清單決定要不要動手的。"""
    assert rr.role_of_row(row(1, name)) == rr.ROLE_BROWSER


def test_an_unrelated_process_is_not_ours():
    assert rr.role_of_row(row(1, "notepad.exe")) is None
    # Python 行程但跑的不是本專案的腳本：一樣不是我們的。
    assert rr.role_of_row(row(1, "python.exe", script="someone_else.py")) is None


def test_the_process_asking_the_question_is_the_bot_even_without_a_cmdline():
    """打包成單一執行檔、或用 `-m` 啟動時命令列比對會失手，而失手的樣子是報告裡
    沒有 bot 那一行——看起來像 bot 沒在跑。"""
    roles = rr.classify([row(7, "frozen.exe")], own_pid=7)
    assert roles == {7: rr.ROLE_BOT}


def test_a_tracked_backend_child_and_its_whole_subtree_are_the_assistant():
    rows = [row(1, script="discord_bot.py"),
            row(2, "node.exe", ppid=1),
            row(3, "bash.exe", ppid=2),
            row(4, "conhost.exe", ppid=3)]
    roles = rr.classify(rows, assistant_pids=[2])
    assert roles == {1: rr.ROLE_BOT, 2: rr.ROLE_ASSISTANT,
                     3: rr.ROLE_ASSISTANT, 4: rr.ROLE_ASSISTANT}


def test_an_untracked_child_of_the_bot_is_not_called_the_assistant():
    """bot 也會生 `taskkill`／`git` 這種一閃即逝的行程。把它們算成「對話助理的
    後端」不會讓總數變錯，但會讓那一格說謊——而那一格正是擁有者拿來判斷「後端是不
    是又在吃記憶體」的數字。"""
    rows = [row(1, script="discord_bot.py"), row(2, "taskkill.exe", ppid=1)]
    roles = rr.classify(rows, assistant_pids=[])
    assert roles[2] == rr.ROLE_CHILD


def test_a_descendant_map_answers_the_same_question_as_the_ppid_chain():
    """正式路徑用 `children(recursive=True)` 的結果，測試路徑用 `ppid` 鏈；兩條
    必須給同一個答案，否則量測快的那一條會跟守門看的那一條分家。"""
    rows = [row(1, script="discord_bot.py"), row(9, "node.exe")]
    by_map = rr.classify(rows, descendant_roots={9: 1})
    by_chain = rr.classify([rows[0], row(9, "node.exe", ppid=1)])
    assert by_map == by_chain == {1: rr.ROLE_BOT, 9: rr.ROLE_CHILD}


def test_a_broken_parent_chain_does_not_hang():
    """`ppid` 讀的是別的行程正在改的活資料，號碼被回收之後有機會繞成環。上限的
    代價只是極深的樹判不出祖先；沒有上限的代價是這支指令永遠不回來。"""
    rows = [row(1, script="discord_bot.py"),
            row(50, "a.exe", ppid=51), row(51, "b.exe", ppid=50)]
    roles = rr.classify(rows)
    assert roles == {1: rr.ROLE_BOT}


def test_a_batch_under_the_bot_stays_the_batch():
    """批次是 bot 生的，所以它同時也是「bot 的後代」。先判腳本名那一段，否則整棵
    瀏覽器會被算進「其他子行程」，而「背景產圖程式」那一行會消失。"""
    rows = [row(1, script="discord_bot.py"),
            row(2, script="webrunner_novelai.py", ppid=1),
            row(3, "chrome.exe", ppid=2)]
    roles = rr.classify(rows)
    assert roles[2] == rr.ROLE_BATCH
    assert roles[3] == rr.ROLE_BROWSER


# --------------------------------------------------------------------------
# 2. 合計
# --------------------------------------------------------------------------
def test_a_field_that_could_not_be_read_is_not_counted_as_zero():
    rows = [row(1, script="discord_bot.py", rss=100, cpu=1.0),
            row(2, script="discord_bot.py", rss=None, cpu=None)]
    usage = rr.summarise(rows, rr.classify(rows))[rr.ROLE_BOT]
    assert usage.rss == 100 and usage.cpu == 1.0
    assert usage.missing_rss == 1 and usage.missing_cpu == 1


def test_a_role_whose_every_field_is_unreadable_reports_nothing_not_zero():
    """`None` 與 `0` 在畫面上差很多：一個是「讀不到」，一個是「真的沒在吃」。"""
    rows = [row(1, script="discord_bot.py", rss=None, cpu=None)]
    usage = rr.summarise(rows, rr.classify(rows))[rr.ROLE_BOT]
    assert usage.rss is None and usage.cpu is None


def test_a_virtualenv_stub_pair_counts_as_one_instance_but_two_processes():
    """轉接殼不是多一個實例（否則健康報告會永遠說多了一個孤兒），但它真的佔
    記憶體，所以行程數與記憶體都要算它。"""
    rows = [row(10, script="webrunner_novelai.py", rss=5),
            row(11, script="webrunner_novelai.py", ppid=10, rss=7)]
    usage = rr.summarise(rows, rr.classify(rows))[rr.ROLE_BATCH]
    assert (usage.logical, usage.procs, usage.rss) == (1, 2, 12)


def test_the_browser_tree_is_never_collapsed_into_one():
    """併轉接殼的規則是「父行程也在名單裡就丟掉」，而瀏覽器是一整棵同名的父子樹。
    套上去的話 24 個 chrome 會被併成 1 個，報告會說瀏覽器只開了一個。"""
    rows = [row(20, "chrome.exe", rss=1),
            row(21, "chrome.exe", ppid=20, rss=1),
            row(22, "chrome.exe", ppid=21, rss=1)]
    usage = rr.summarise(rows, rr.classify(rows))[rr.ROLE_BROWSER]
    assert usage.logical == usage.procs == 3


def test_the_oldest_process_in_a_role_is_the_one_reported():
    """「批次已經跑了十四小時」是擁有者真正會問的那個數字，所以取最早的那一個，
    不是最晚的、也不是平均。"""
    rows = [row(1, script="webrunner_novelai.py", started=5000.0),
            row(2, script="webrunner_je_only.py", started=1000.0)]
    usage = rr.summarise(rows, rr.classify(rows))[rr.ROLE_BATCH]
    assert usage.oldest_started == 1000.0


def test_a_start_time_that_is_not_a_number_is_ignored_rather_than_believed():
    rows = [row(1, script="discord_bot.py", started=None),
            row(2, script="start_webrunner.py", started=True)]
    roles = rr.summarise(rows, rr.classify(rows))
    assert roles[rr.ROLE_BOT].oldest_started is None
    assert roles[rr.ROLE_LAUNCHER].oldest_started is None


def test_roles_come_back_in_the_declared_order():
    """回覆是一行一個角色，順序由「本體 → 它生出來的東西」決定；`dict` 的插入
    順序就是渲染順序，所以這件事要釘住。"""
    rows = [row(3, "chrome.exe"), row(1, script="discord_bot.py"),
            row(2, script="webrunner_novelai.py")]
    got = list(rr.summarise(rows, rr.classify(rows)))
    assert got == [rr.ROLE_BOT, rr.ROLE_BATCH, rr.ROLE_BROWSER]


def test_every_role_has_a_label_and_a_place_in_the_order():
    """少一個標籤的症狀是回覆裡冒出 `child` 這種代號，而不是報錯。"""
    assert set(rr.ROLE_ORDER) == set(rr.ROLE_LABELS)
    assert set(rr.SCRIPT_ROLES.values()) <= set(rr.ROLE_ORDER)


# --------------------------------------------------------------------------
# 3. 降級：每一種失敗都變成「缺一格 ＋ 一句說明」，不是例外、也不是錯的數字
# --------------------------------------------------------------------------
def test_a_scan_that_did_not_finish_says_so():
    snap = rr.collect(disk_path=".", own_pid=None,
                      scan=scan_of([], scan_ok=False,
                                   notes=[("scan_incomplete", None)]),
                      machine=machine_of())
    assert snap.scan_ok is False
    assert ("scan_incomplete", None) in snap.notes


def test_a_scan_that_raises_degrades_instead_of_killing_the_command():
    def boom():
        raise RuntimeError("process table went away")

    snap = rr.collect(disk_path=".", scan=boom, machine=machine_of())
    assert snap.roles == {} and snap.scan_ok is False
    assert ("scan_incomplete", None) in snap.notes


def test_machine_facts_that_raise_degrade_on_their_own():
    """三件事各自失敗、各自降級。綁在一起的話，一顆查不到的磁碟會連帶讓記憶體
    那一行消失，而使用者看到的是「沒講」——跟「這台機器沒有壓力」長得一樣。"""
    def boom():
        raise OSError("no such drive")

    snap = rr.collect(disk_path=".",
                      scan=scan_of([row(1, script="discord_bot.py")]),
                      machine=boom)
    assert snap.mem_total is None and snap.disk_total is None
    assert snap.roles[rr.ROLE_BOT].procs == 1          # 行程那半沒被拖下水
    keys = {key for key, _detail in snap.notes}
    assert {"memory_unavailable", "disk_unavailable"} <= keys


def test_the_same_note_is_not_said_twice():
    snap = rr.collect(disk_path=".",
                      scan=scan_of([], scan_ok=False,
                                   notes=[("scan_incomplete", None)]),
                      machine=machine_of(notes=[("scan_incomplete", None)]))
    keys = [key for key, _detail in snap.notes]
    assert keys.count("scan_incomplete") == 1


def test_processes_that_vanished_or_were_denied_are_counted_and_reported():
    snap = rr.collect(disk_path=".",
                      scan=scan_of([], notes=[("gone", 3), ("denied", 2)]),
                      machine=machine_of())
    assert dict(snap.notes)["gone"] == 3
    assert dict(snap.notes)["denied"] == 2


def test_no_psutil_reports_a_missing_scan_rather_than_an_empty_machine(monkeypatch):
    """缺了 psutil 與「這台機器上什麼都沒在跑」回的都是空名單，兩者在畫面上
    分不出來——所以那件事必須自己講出來。

    `sys.modules["psutil"] = None` 是「讓 `import` 丟 ImportError」的標準寫法，
    而還原一定要走 `monkeypatch`：自己寫 `sys.modules.pop` 清掉的是**快取**，
    下一次 `import` 會從磁碟載入真的那一份，等於把測試的隔離網拆掉
    （`test_suite_safety.test_no_test_can_reach_the_real_machine` 擋的就是這個
    寫法：一個真行程都不該被測試碰到，否則正式批次會被誤傷）。"""
    monkeypatch.setitem(sys.modules, "psutil", None)
    result = rr._psutil_rows(own_pid=1, cpu_interval=0.0)
    assert result.rows == () and result.scan_ok is False
    assert ("psutil_missing", None) in result.notes


def test_an_injected_scan_does_not_invent_this_machines_own_pid():
    """注入假行程表的測試不該憑空多出一個真 pid——撞上的機率很低，但撞上時的
    症狀（某一列突然變成 `bot`）看起來會像判定邏輯壞了。"""
    import os
    rows = [row(os.getpid(), "notepad.exe")]
    snap = rr.collect(disk_path=".", scan=scan_of(rows), machine=machine_of())
    assert snap.roles == {}


def test_the_snapshot_says_how_long_it_took():
    """成本要看得見：這支比其他狀態指令慢，慢在哪裡必須印在回覆裡。"""
    snap = rr.collect(disk_path=".", scan=scan_of([]), machine=machine_of(),
                      cpu_interval=0.25)
    assert snap.elapsed >= 0.0 and snap.cpu_interval == 0.25


# --------------------------------------------------------------------------
# 4. 送出面：秘密分層 1
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def bot():
    import discord_bot                                # noqa: PLC0415
    return discord_bot


def _asker(bot_module, *, owner):
    uid = bot_module.OWNER_USER_ID if owner else bot_module.OWNER_USER_ID + 1
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid))


def _sample_snapshot():
    rows = [row(4321, script="discord_bot.py", rss=30 * 1024 ** 2, cpu=1.0,
                started=0.0),
            row(9876, "chrome.exe", rss=400 * 1024 ** 2, cpu=2.0, started=0.0)]
    return rr.collect(disk_path=r"D:\somewhere\output",
                      scan=scan_of(rows), machine=machine_of(), clock=lambda: 3600.0)


def test_the_owner_sees_pids_the_real_names_and_the_real_path(bot):
    text = bot._resource_report_text(_sample_snapshot(),
                                     _asker(bot, owner=True), now=3600.0)
    assert "4321" in text and "9876" in text
    assert "chrome.exe" in text
    assert r"D:\somewhere\output" in text


def test_nobody_else_sees_a_pid_a_host_path_or_a_real_process_name(bot):
    text = bot._resource_report_text(_sample_snapshot(),
                                     _asker(bot, owner=False), now=3600.0)
    for leaked in ("4321", "9876", "chrome.exe", "somewhere", "D:\\"):
        assert leaked not in text, f"外洩：{leaked}"
    assert "瀏覽器" in text            # 泛用標籤還是要講得出是什麼


def test_an_asker_we_cannot_identify_is_treated_as_a_stranger(bot):
    """取不到提問者 UID 一律 fail-closed（`_owner_detail` 的既有合約）。"""
    text = bot._resource_report_text(_sample_snapshot(), object(), now=3600.0)
    assert "4321" not in text and r"D:\somewhere\output" not in text


def test_the_report_points_at_the_two_existing_reports_instead_of_repeating_them(bot):
    """分工要寫在**回覆裡**，不是只寫在註解裡。順手把批次進度也印進來是每一份狀態
    報告都會長出來的東西，而長出來之後兩邊就開始各講一半。"""
    text = bot._resource_report_text(_sample_snapshot(),
                                     _asker(bot, owner=True), now=3600.0)
    assert "/gen current" in text and "/dorossi running" in text


def test_a_role_with_no_readable_numbers_renders_a_question_mark_not_a_zero(bot):
    rows = [row(1, script="discord_bot.py", rss=None, cpu=None, started=None)]
    snap = rr.collect(disk_path=".", scan=scan_of(rows), machine=machine_of(),
                      clock=lambda: 0.0)
    text = bot._resource_report_text(snap, _asker(bot, owner=True), now=0.0)
    assert "記憶體 `?`" in text


def test_every_degradation_note_has_a_sentence(bot):
    """鍵在 `_resource_report`、句子在 `discord_bot`。漏一句的症狀是那筆降級**安靜
    消失**——使用者看到的畫面跟「一切正常」一模一樣。"""
    produced = {"psutil_missing", "scan_incomplete", "gone", "denied",
                "cpu_unavailable", "memory_unavailable", "disk_unavailable"}
    assert produced == set(bot._RESOURCE_NOTE_TEXT)


def test_a_note_that_carries_a_count_renders_that_count(bot):
    snap = rr.collect(disk_path=".", scan=scan_of([], notes=[("denied", 4)]),
                      machine=machine_of(), clock=lambda: 0.0)
    text = bot._resource_report_text(snap, _asker(bot, owner=True), now=0.0)
    assert "4 個行程沒有權限讀" in text


def test_a_per_process_cpu_figure_is_converted_to_a_share_of_the_machine(bot):
    """psutil 的口徑是「一顆核心 ＝ 100%」，照抄出去會讓一個吃滿三顆核心的後端
    看起來像「這台機器炸了 300%」。核心數問不到時退回原始數字並標成 `/核`——
    **不要**猜一個核心數去除，猜錯的百分比看起來跟真的一樣。"""
    assert bot._resource_cpu_text(300.0, 12) == "25.0%"
    assert bot._resource_cpu_text(300.0, None) == "300.0%/核"
    assert bot._resource_cpu_text(None, 12) == "?"


def test_the_backend_pid_list_skips_children_that_already_exited(bot):
    """結束的行程 pid 有機會已經被回收再發給別人，把別人的行程算成我們的比少算
    一筆難看得多。"""
    live = types.SimpleNamespace(pid=111, returncode=None)
    dead = types.SimpleNamespace(pid=222, returncode=0)
    turns = bot._dorossi_turns
    saved = list(turns)
    turns.clear()
    turns.extend([types.SimpleNamespace(proc=live),
                  types.SimpleNamespace(proc=dead),
                  types.SimpleNamespace(proc=None)])
    try:
        assert bot._dorossi_backend_pids() == [111]
    finally:
        turns.clear()
        turns.extend(saved)


def test_the_handler_replies_generically_when_the_whole_snapshot_fails(bot):
    """診斷指令自己炸掉，等於在最需要資訊的時候什麼都不說。"""
    sent = []

    async def fake_reply(_message, text, **_kwargs):
        sent.append(text)

    def boom(*_args, **_kwargs):
        raise RuntimeError(r"D:\secret\path exploded")

    saved_reply = bot.safe_reply
    saved_collect = bot._resource_report.collect
    bot.safe_reply = fake_reply
    bot._resource_report.collect = boom
    try:
        asyncio.run(bot.cmd_proc_usage(_asker(bot, owner=False)))
    finally:
        bot.safe_reply = saved_reply
        bot._resource_report.collect = saved_collect
    assert sent and "secret" not in sent[0]
