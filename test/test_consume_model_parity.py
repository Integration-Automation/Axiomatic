"""動態消耗的**模型**與真的 `run_batch` 迴圈，要對同一組佇列給出同一個答案。

`_queue_consume.simulate` 是動態消耗的模型：哪一對先產、哪一筆該 pop、收工時磁碟
長什麼樣。它在產品路徑上**沒有任何呼叫端**——它的用途是當這套設計的可執行規格，
`test_dynamic_consume` 拿它跟 `reference_snapshot`（**另一個模型**）逐筆對拉。

於是這裡有三份東西在講同一條規則：`simulate`、`reference_snapshot`，以及真正會
動磁碟的 `_webrunner_shared.run_batch`。**前兩份互相對拉，第三份沒有人跟它比。**
這正是本 repo 記過的形狀——兩份各自全綠的實作，中間沒有東西在比較它們——而且這一
組的賭注不小：模型說的話會變成 bot 的 `/gen plan` 預覽與 `/eta`，使用者照著它排
自己的佇列。

這支測試把同一組佇列同時餵給兩邊，比**批序**與**收工時的磁碟狀態**。語料三類：

1. 基本形狀（補位、fallback、`end` 哨符、位置性的空行）；
2. 未達門檻（保留在佇列前端、游標越過）——`min_save_ratio` 那條路；
3. run 中編輯佇列——`simulate` 的 `edits`，也是這支測試最花力氣的一類。

2026-09-21 第一次跑：18 個形狀全部一致。**這是一份「現在是對的」的證據，不是
「以後不會壞」的保證**——所以它留在這裡當守門。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _queue_consume as qc  # noqa: E402
import _webrunner_shared as ws  # noqa: E402
from test_webrunner_shared import FakeBrowserPort, _RunBatchHarness  # noqa: E402

QUEUE_FILES = {"p": "todo_prompt.md", "1": "todo_character1.md",
               "2": "todo_character2.md", "u": "todo_undesired.md"}
FALLBACK_FILES = {"p": "prompt.md", "1": "character1.md",
                  "2": "character2.md", "u": "undesired.md"}


def _raw_lines(path: Path) -> list[str]:
    """原樣讀，**保留空行**。

    `_RunBatchHarness.read_lines` 會把空白行濾掉，而 `todo_character2.md` 是位置性
    的——空行的意思是「這一對停用角色 2」。用那一支讀會把這個測試最想看的差別洗掉。
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _run_the_real_loop(queues, fallbacks, fail_set, edits):
    """跑真的 `run_batch`，回 `(批序, 收工時的四個佇列, 真的套用到的編輯)`。

    批序不是從 `gen_calls` 直接拿的：迴圈對「跟上一對相同的欄位」會**跳過重填**
    （per-pair diff），所以逐次記錄填了什麼會漏掉沒變的欄位。這裡改成維護一份
    「瀏覽器現在顯示什麼」的狀態，由四個填值替身更新，在每次 `generate_loop` 時
    取一張快照——那才是這一對真正送出去的四個欄位。
    """
    with _RunBatchHarness() as h:
        for key, name in QUEUE_FILES.items():
            h.write_queue(name, queues.get(key, []))
        for key, name in FALLBACK_FILES.items():
            if fallbacks.get(key) is not None:
                (h.dir / name).write_text(fallbacks[key], encoding="utf-8")
        # 對帳會把原磁碟內容備份到 `<PROJECT_ROOT>/.backup/`，而那個目的地是在函式
        # 裡用 `PROJECT_ROOT` 現組的。不導的話這支會真的寫進 repo。
        h.patch("PROJECT_ROOT", h.dir)

        state = {"p": "", "1": "", "2": "", "u": ""}
        pairs: list[tuple[str, str, str, str]] = []
        applied: set[int] = set()

        def _fill_main(_port, value):
            state["p"] = value
            return True

        def _fill_undesired(_port, value):
            state["u"] = value
            return True

        def _fill_character(_port, index, value):
            state[str(index)] = value
            return True

        def _set_character2(_port, enabled):
            if not enabled:
                state["2"] = ""
            return True

        def _generate(_port, _name, *_args, **_kwargs):
            index = len(pairs)
            pairs.append((state["p"], state["1"], state["2"], state["u"]))
            return 0 if index in fail_set else 999

        def _at_pair_boundary(_where):
            """run 中編輯的落點：每圈的第一行，`read_queues()` **之前**。

            ⚠️ 這個落點不是隨便挑的，前兩版都挑錯，而且錯法不一樣：

            * 第一版掛在 `fill_main_prompt`——那是 `decide` **之後**，所以編輯永遠
              晚一圈才被看到。
            * 第二版改成包住 `_queue_consume.decide` 本身，以為「在 decide 前面做
              事」就對了。**還是太晚**：迴圈是先 `read_queues()` 把四個清單讀好、
              再當引數傳進 `decide`，所以包住消費端根本改不到它的輸入。

            兩版各自量出 3～4 筆「不一致」，全部是探針自己造出來的。
            """
            index = len(pairs)
            if index in edits and index not in applied:
                applied.add(index)
                for key, lines in edits[index].items():
                    h.write_queue(QUEUE_FILES[key], lines)

        h.patch("fill_main_prompt", _fill_main)
        h.patch("fill_main_undesired", _fill_undesired)
        h.patch("fill_character_prompt", _fill_character)
        h.patch("set_character2_enabled", _set_character2)
        h.patch("generate_loop", _generate)
        h.patch("wait_if_paused", _at_pair_boundary)

        ws.run_batch(FakeBrowserPort(), "email", "pw",
                     setup_fn=lambda: True, minimize_fn=lambda: None)
        final = {key: _raw_lines(h.dir / name)
                 for key, name in QUEUE_FILES.items()}
        stopped_by_end = any(
            kw.get("stopped_by_end") for kw in h.events_of("todo_done"))
    return pairs, final, applied, stopped_by_end


def _ask_the_model(queues, fallbacks, fail_set, edits):
    batches, final, stopped = qc.simulate(
        real_p=list(queues.get("p", [])),
        real_1=list(queues.get("1", [])),
        real_2=list(queues.get("2", [])),
        real_u=list(queues.get("u", [])),
        fb_p_text=fallbacks.get("p"),
        fb_1_text=fallbacks.get("1"),
        fb_2_text=fallbacks.get("2"),
        fb_u_text=fallbacks.get("u"),
        fail_set=set(fail_set) or None,
        edits={index: {f"real_{key}": list(lines)
                       for key, lines in edit.items()}
               for index, edit in edits.items()} or None,
    )
    return list(batches), dict(zip(("p", "1", "2", "u"), final)), stopped


def _parity_problems(real, model) -> list[str]:
    """比較邏輯自己一支，才有辦法對它做對照組。

    語料乾淨的時候「回報不一致」那幾行一次都不會執行，整段刪掉照樣全綠。
    """
    real_pairs, real_final, real_stopped = real
    model_batches, model_final, model_stopped = model
    problems: list[str] = []
    if list(real_pairs) != list(model_batches):
        problems.append(
            f"批序不同\n    真的={list(real_pairs)}\n    模型={list(model_batches)}")
    for key in ("p", "1", "2", "u"):
        if list(real_final[key]) != list(model_final[key]):
            problems.append(
                f"{QUEUE_FILES[key]} 收工狀態不同："
                f"真的={list(real_final[key])} 模型={list(model_final[key])}")
    if bool(real_stopped) != bool(model_stopped):
        problems.append(
            f"stopped_by_end 不同：真的={real_stopped} 模型={model_stopped}")
    return problems


# (名稱, 四個佇列, fallback, 未達門檻的批序號, run 中編輯)
_CASES = [
    ("四清單等長",
     {"p": ["P1", "P2"], "1": ["a", "b"], "2": ["c", "d"], "u": ["u1", "u2"]},
     {}, set(), {}),
    ("短 prompt 補位", {"p": ["P"], "1": ["a", "b", "c"]}, {}, set(), {}),
    ("短 char1 補位", {"p": ["P1", "P2", "P3"], "1": ["a"]}, {}, set(), {}),
    ("char2 有空行（位置性）",
     {"p": ["P1", "P2"], "1": ["a", "b"], "2": ["c", ""]}, {}, set(), {}),
    ("prompt 走 fallback", {"1": ["a", "b"]}, {"p": "FALLBACK-P"}, set(), {}),
    ("undesired 走 fallback",
     {"p": ["P1"], "1": ["a"]}, {"u": "FALLBACK-U"}, set(), {}),
    ("end 哨符在中間",
     {"p": ["P1", "end", "P3"], "1": ["a", "b", "c"]}, {}, set(), {}),
    ("只有 char1", {"1": ["a", "b", "c"]}, {}, set(), {}),
    # 這兩格的重點是**收工時 char2 還留著空行**。少了它們，`_raw_lines` 改成
    # 「把空行濾掉」不會有任何症狀（變異實測：SURVIVED）——位置性的那條規則
    # 只有在空行真的活到最後時才看得出來。
    ("end 停在中間，char2 留著一個開頭的空行",
     {"p": ["P1", "end", "P3"], "1": ["a", "b", "c"], "2": ["x", "", "z"]},
     {}, set(), {}),
    ("第一對未達門檻，char2 只剩一個空行",
     {"p": ["P1", "P2"], "1": ["a", "b"], "2": ["", "z"]}, {}, {0}, {}),
    # ---- 未達門檻：保留在佇列前端、游標越過 ----
    ("第一對未達門檻",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, {0}, {}),
    ("中間那對未達門檻",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, {1}, {}),
    ("最後一對未達門檻",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, {2}, {}),
    ("連續兩對未達門檻",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, {0, 1}, {}),
    ("補位 ＋ 第一對未達門檻",
     {"p": ["P"], "1": ["a", "b", "c"]}, {}, {0}, {}),
    # ---- run 中編輯 ----
    ("產第 2 對之前，char1 尾端新增一筆",
     {"p": ["P1", "P2"], "1": ["a", "b"]}, {}, set(), {1: {"1": ["b", "c"]}}),
    ("產第 2 對之前，char1 整個換掉",
     {"p": ["P1", "P2"], "1": ["a", "b"]}, {}, set(), {1: {"1": ["z"]}}),
    ("產第 2 對之前，char1 前面插一筆",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, set(),
     {1: {"1": ["新的", "b", "c"]}}),
    ("產第 2 對之前，prompt 佇列長出兩筆",
     {"p": ["P1", "P2"], "1": ["a", "b"]}, {}, set(),
     {1: {"p": ["P2", "P3", "P4"]}}),
    # ⚠️ 這一格原本是「char1 刪到只剩一筆」，而那在**沒有編輯**時本來就會發生
    # （a、b 已經 pop 掉）——加不加那筆編輯批序完全一樣，等於一格裝飾。底下的
    # `test_each_mid_run_edit_actually_changes_the_outcome` 當場把它抓出來。
    ("產第 3 對之前，char1 尾端再長一筆",
     {"p": ["P1", "P2", "P3"], "1": ["a", "b", "c"]}, {}, set(),
     {2: {"1": ["c", "d"]}}),
]

_IDS = [case[0] for case in _CASES]


@pytest.mark.parametrize("label,queues,fallbacks,fail_set,edits",
                         _CASES, ids=_IDS)
def test_the_model_predicts_what_the_real_loop_does(
        label, queues, fallbacks, fail_set, edits):
    """同一組佇列，模型與真的迴圈要給同一個答案。"""
    real_pairs, real_final, applied, real_stopped = _run_the_real_loop(
        queues, fallbacks, fail_set, edits)
    assert real_pairs, f"「{label}」一對都沒產出——這一格什麼都沒比到"
    assert set(edits) == applied, (
        f"「{label}」有編輯沒套用到（落點不存在）：{sorted(set(edits) - applied)}。"
        "注入了卻沒跑，綠得跟通過一模一樣。")
    problems = _parity_problems(
        (real_pairs, real_final, real_stopped),
        _ask_the_model(queues, fallbacks, fail_set, edits))
    assert not problems, (
        f"「{label}」：模型與真的迴圈分家了\n  " + "\n  ".join(problems)
        + "\n模型說的話會變成 bot 的 `/gen plan` 預覽與 `/eta`，"
          "所以分家的代價是使用者照著一份假的計畫排佇列。")


@pytest.mark.parametrize("label,queues,fallbacks,fail_set,edits",
                         [c for c in _CASES if c[4]],
                         ids=[c[0] for c in _CASES if c[4]])
def test_each_mid_run_edit_actually_changes_the_outcome(
        label, queues, fallbacks, fail_set, edits):
    """每一筆 run 中編輯都要**真的改變結果**，否則那一格是裝飾。

    一個不影響批序的編輯，在上面那支測試裡跟「沒有編輯」長得一模一樣：兩邊都算得
    出同一個答案，於是 parity 通過，而 `edits` 那條路一行都沒被驗到。
    """
    with_edit, _final, _applied, _stopped = _run_the_real_loop(
        queues, fallbacks, fail_set, edits)
    without, _f2, _a2, _s2 = _run_the_real_loop(queues, fallbacks, fail_set, {})
    assert with_edit != without, (
        f"「{label}」加不加這筆編輯，批序完全一樣（{with_edit}）——"
        "這一格證明不了 `edits` 那條路有被走到")


def test_the_corpus_covers_all_three_kinds():
    """語料下限：三類各要有幾個，否則這支測試會在某一類被清空時安靜縮水。"""
    plain = [c for c in _CASES if not c[3] and not c[4]]
    threshold = [c for c in _CASES if c[3]]
    edited = [c for c in _CASES if c[4]]
    assert len(plain) >= 6, f"基本形狀只剩 {len(plain)} 個"
    assert len(threshold) >= 4, f"未達門檻只剩 {len(threshold)} 個"
    assert len(edited) >= 4, f"run 中編輯只剩 {len(edited)} 個"
    assert len(_IDS) == len(set(_IDS)), "語料有重複的名字"


_BROKEN_PREMISES = [
    # (要出現在訊息裡的字眼, 佇列, fallback, 未達門檻, 編輯)
    ("一對都沒產出", {"p": [], "1": [], "2": [], "u": []}, {}, set(), {}),
    ("落點不存在", {"p": ["P1"], "1": ["a"]}, {}, set(), {99: {"1": ["x"]}}),
]


@pytest.mark.parametrize("phrase,queues,fallbacks,fail_set,edits",
                         _BROKEN_PREMISES,
                         ids=[row[0] for row in _BROKEN_PREMISES])
def test_each_premise_check_fires_on_its_own_broken_case(
        phrase, queues, fallbacks, fail_set, edits):
    """上面那支測試的兩道前提檢查，各要有自己的紅燈。

    為什麼需要：今天的語料裡**沒有**一格是空的、也**沒有**一筆編輯落空，所以那兩
    行斷言在真實資料上永遠不會開火——變異實測，兩個都活了下來。一道永遠不開火的
    前提檢查，跟沒有那行是同一件事。

    兩格刻意用**不同的字眼** match：共用一句話的話，拿掉其中一道還是會被另一道
    的訊息接住，於是有一半是裝飾。
    """
    with pytest.raises(AssertionError, match=phrase):
        test_the_model_predicts_what_the_real_loop_does(
            "對照組", queues, fallbacks, fail_set, edits)


def test_the_comparison_notices_a_different_batch_order():
    """對照組一：批序不同要抓得到。"""
    empty = {"p": [], "1": [], "2": [], "u": []}
    problems = _parity_problems(
        ([("P1", "a", "", "")], empty, False),
        ([("P1", "b", "", "")], empty, False))
    assert any("批序不同" in p for p in problems), problems


def test_the_comparison_notices_a_different_final_queue():
    """對照組二：批序一樣、收工狀態不同，也要抓得到。

    兩個斷言各要有自己的破綻——共用一格的話，拿掉其中一半還是綠的。
    """
    same = [("P1", "a", "", "")]
    problems = _parity_problems(
        (same, {"p": ["P2"], "1": [], "2": [], "u": []}, False),
        (same, {"p": [], "1": [], "2": [], "u": []}, False))
    assert any("todo_prompt.md" in p for p in problems), problems
    assert not any("批序不同" in p for p in problems), problems


def test_the_comparison_notices_a_different_end_verdict():
    """對照組三：`stopped_by_end` 是給監督者看的（rc=0 不要重生），不能漏比。"""
    same = [("P1", "a", "", "")]
    empty = {"p": [], "1": [], "2": [], "u": []}
    problems = _parity_problems((same, empty, True), (same, empty, False))
    assert any("stopped_by_end" in p for p in problems), problems


def test_a_matching_pair_reports_nothing():
    """對照組的反面：一致的時候不得報任何東西（否則上面三支恆真）。"""
    same = [("P1", "a", "", "")]
    final = {"p": ["P2"], "1": [], "2": [], "u": []}
    assert _parity_problems((same, final, True), (same, final, True)) == []
