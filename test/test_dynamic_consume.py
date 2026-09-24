"""動態佇列消耗的純函式測試（無 Selenium、無磁碟 I/O）。

驗證 `_queue_consume.simulate`（動態逐批重讀模型）：
1. 等價性：沒有 mid-run 編輯、沒有 below-threshold 時，批序與最終佇列狀態必須與
   舊「啟動快照 + _can_pop front-pop」參考模型（`reference_snapshot`）逐筆相同。
2. below-threshold：失敗批保留在前端、後續批照常消耗、留待下輪。
3. growth / 重排：run 中對「尚未輪到」的佇列做新增 / 刪除，下一批反映磁碟。

可直接 `py -3 test/test_dynamic_consume.py`（自帶 runner），也可 pytest。
"""

import ast as _ast
import pathlib as _pathlib
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _queue_consume as qc  # noqa: E402


# ---------- 等價性矩陣 ------------------------------------------------------

def _assert_equiv(name, **kw):
    """同一組佇列形狀：simulate（無 fail / 無 edit）必須等於 reference_snapshot。"""
    dyn = qc.simulate(**kw)
    ref = qc.reference_snapshot(**kw)
    assert dyn[0] == ref[0], (
        f"[{name}] 批序不同\n  dynamic={dyn[0]}\n  ref    ={ref[0]}")
    assert dyn[1] == ref[1], (
        f"[{name}] 最終佇列不同\n  dynamic={dyn[1]}\n  ref    ={ref[1]}")
    assert dyn[2] == ref[2], f"[{name}] stopped_by_end 不同"
    print(f"  OK equiv: {name} — {len(dyn[0])} batch(es), final={dyn[1]}")


def test_equivalence_matrix():
    print("test_equivalence_matrix:")
    # 四清單等長。
    _assert_equiv("equal-length",
                  real_p=["P1", "P2"], real_1=["a", "b"],
                  real_2=["c", "d"], real_u=["u1", "u2"])
    # 短 prompt [P] × 長 char1 [a,b,c]（padding）。
    _assert_equiv("short-prompt-padding",
                  real_p=["P"], real_1=["a", "b", "c"],
                  real_2=[], real_u=[])
    # 短 char2 / undesired padding 與長 char1。
    _assert_equiv("short-char2-padding",
                  real_p=["P1", "P2", "P3"], real_1=["a", "b", "c"],
                  real_2=["x"], real_u=["u"])
    # prompt fallback。
    _assert_equiv("prompt-fallback",
                  real_p=[], real_1=["a", "b"], real_2=[], real_u=[],
                  fb_p_text="FALLBACK_PROMPT")
    # char1 fallback（2026-08-23 接上）。
    _assert_equiv("char1-fallback",
                  real_p=["P1", "P2"], real_1=[], real_2=[],
                  real_u=[], fb_1_text="FALLBACK_CHAR1")
    # char2 fallback。
    _assert_equiv("char2-fallback",
                  real_p=["P1", "P2"], real_1=["a", "b"], real_2=[],
                  real_u=[], fb_2_text="FALLBACK_CHAR2")
    # undesired fallback。
    _assert_equiv("undesired-fallback",
                  real_p=["P1", "P2"], real_1=["a", "b"], real_2=["c", "d"],
                  real_u=[], fb_u_text="FALLBACK_UNDESIRED")
    # 全 fallback（real 全空、四個 fallback）→ 必須剛好 1 批。
    dyn = qc.simulate(real_p=[], real_1=[], real_2=[], real_u=[],
                      fb_p_text="FP", fb_1_text="FC1", fb_2_text="FC2",
                      fb_u_text="FU")
    assert len(dyn[0]) == 1, f"all-fallback 應只產 1 批，得 {len(dyn[0])}"
    assert dyn[0][0] == ("FP", "FC1", "FC2", "FU"), dyn[0]
    # 全 fallback 最終佇列維持空（沒有真的東西被 pop）。
    assert dyn[1] == ([], [], [], []), dyn[1]
    print(f"  OK all-fallback-only — 1 batch {dyn[0][0]}")
    # end sentinel 在中間：[P1, end, P3] × char1 三筆。
    _assert_equiv("end-sentinel-mid",
                  real_p=["P1", "end", "P3"], real_1=["a", "b", "c"],
                  real_2=[], real_u=[])
    print("  PASS\n")


def test_all_fallback_does_not_resurrect():
    """真實佇列抽乾後，fallback（長度 1）不可生出多餘幽靈角色。
    char1 有 1 筆真實工作 + prompt fallback：只應產 1 批，產完 char1 空、收工。"""
    print("test_all_fallback_does_not_resurrect:")
    dyn = qc.simulate(real_p=[], real_1=["only"], real_2=[], real_u=[],
                      fb_p_text="FP")
    assert dyn[0] == [("FP", "only", "", "")], dyn[0]
    assert dyn[1] == ([], [], [], []), dyn[1]
    print(f"  OK — 1 batch {dyn[0][0]}, final empty")
    print("  PASS\n")


def test_an_empty_fallback_ends_the_run_instead_of_crashing():
    """真實佇列全空、fallback「作用中」卻一筆都沒有（fallback 檔是空的）：`pair_todos` 算出
    零批，這時要收工，不能去取 `pairs[0]`——那會讓整個批次以 IndexError 結束，而使用者看到
    的是一次崩潰，不是「佇列空了」。"""
    print("test_an_empty_fallback_ends_the_run_instead_of_crashing:")
    for fb in ((True, False, False, False), (False, True, False, False),
               (True, True, True, True)):
        got = qc.decide([], [], [], [], [], [], [], [], *fb, skip=0, produced=0)
        assert got.action == qc.ACTION_BREAK and got.batch is None, (fb, got)
    # 對照組：fallback 有一筆時照樣產那一個 fallback 角色。
    got = qc.decide([], [], [], [], ["FP"], [], [], [], True, False, False, False,
                    skip=0, produced=0)
    assert got.action == qc.ACTION_FALLBACK_SINGLE and got.batch[0] == "FP", got
    print("  PASS\n")


def test_an_empty_list_never_pops():
    """一條已經空了的清單沒有東西可以 pop。游標算成 `min(skip, -1) = -1` 之後，最後一批的
    `is_last` 會讓它回 True——呼叫端接著去刪第 -1 筆，也就是**別的清單的尾巴**被刪掉的那種
    錯位。"""
    print("test_an_empty_list_never_pops:")
    for skip in (0, 1, 5):
        for is_last in (False, True):
            assert qc.should_pop_at(0, skip, is_last) is False, (skip, is_last)
            assert qc.should_pop_at(-1, skip, is_last) is False, (skip, is_last)
    assert qc.should_pop_at(1, 0, True) is True
    print("  PASS\n")


# ---------- below-threshold ------------------------------------------------

def test_below_threshold_retained():
    """fail_set={1}（第 2 個產出的角色未達門檻）：該筆保留在前端、後續照常消耗、
    留待下輪。char1=[a,b,c]、prompt=[P]（padding）。"""
    print("test_below_threshold_retained:")
    dyn = qc.simulate(real_p=["P"], real_1=["a", "b", "c"],
                      real_2=[], real_u=[], fail_set={1})
    batches, final, _ = dyn
    # 三批都有跑（a, b, c）。
    assert [b[1] for b in batches] == ["a", "b", "c"], batches
    # 'b' 未達門檻 → 留在 char1，待下輪重試。a 已 pop、c 是最後一批也 pop。
    rem_p, rem_1, rem_2, rem_u = final
    assert rem_1 == ["b"], f"char1 應剩 ['b']，得 {rem_1}"
    # prompt=[P] padding：a 完成時 len>1 不 pop（保留尾巴），b 失敗不 pop，
    # c 是最後一批 → is_last pop 掉 P。
    assert rem_p == [], f"prompt 應清空（最後一批 pop），得 {rem_p}"
    print(f"  OK — char1 剩 {rem_1}（保留失敗的 'b'），prompt 剩 {rem_p}")
    print("  PASS\n")


def test_below_threshold_zero_then_retry_next_run():
    """第一批就失敗：保留在前端；模擬「下一輪 run」（用上一輪 final 當輸入、清空
    fail_set）→ 該筆重新被消耗並 pop。"""
    print("test_below_threshold_zero_then_retry_next_run:")
    run1 = qc.simulate(real_p=["P1", "P2"], real_1=["a", "b"],
                       real_2=[], real_u=[], fail_set={0})
    b1, final1, _ = run1
    assert [b[1] for b in b1] == ["a", "b"], b1
    # 第一批 (P1,a) 失敗 → 不 pop；第二批 (P2,b) 成功且是最後一批 → pop b、pop P2。
    rem_p, rem_1, _, _ = final1
    assert rem_1 == ["a"], f"run1 後 char1 應剩 ['a']，得 {rem_1}"
    assert rem_p == ["P1"], f"run1 後 prompt 應剩 ['P1']，得 {rem_p}"
    # 下一輪：重跑，這次都成功。
    run2 = qc.simulate(real_p=rem_p, real_1=rem_1, real_2=[], real_u=[])
    b2, final2, _ = run2
    assert [b[1] for b in b2] == ["a"], b2
    assert final2 == ([], [], [], []), final2
    print(f"  OK — run1 剩 char1={rem_1} prompt={rem_p}；run2 清空")
    print("  PASS\n")


# ---------- growth / 重排 ---------------------------------------------------

def test_growth_append_char1():
    """run 中對 char1 append 一筆（產第 2 個角色前）→ 新條目於本輪被消耗。"""
    print("test_growth_append_char1:")
    # 起始 char1=[a, b]，prompt=[P]。產 a 之後（produced 即將 ==1）append 'c'。
    dyn = qc.simulate(real_p=["P"], real_1=["a", "b"], real_2=[], real_u=[],
                      edits={1: {"real_1": ["b", "c"]}})
    batches, final, _ = dyn
    # 注意：a pop 後磁碟 char1 變 ['b']，edit 把它覆寫成 ['b','c']（模擬使用者
    # 在 'b' 後面加 'c'）→ 本輪應接著產 b、c。
    assert [b[1] for b in batches] == ["a", "b", "c"], batches
    assert final[1] == [], f"char1 應全消耗，得 {final[1]}"
    print(f"  OK — 動態納入新增的 'c'：char1 批序 {[b[1] for b in batches]}")
    print("  PASS\n")


def test_growth_snapshot_would_miss():
    """對照：舊快照模型對同樣形狀（無 edit）只會產 a、b；證明新增條目本來在快照
    模型下會被漏掉，動態模型才補得到。"""
    print("test_growth_snapshot_would_miss:")
    ref = qc.reference_snapshot(real_p=["P"], real_1=["a", "b"],
                                real_2=[], real_u=[])
    assert [b[1] for b in ref[0]] == ["a", "b"], ref[0]
    print(f"  OK — 快照模型只看見啟動當下的 {[b[1] for b in ref[0]]}")
    print("  PASS\n")


def test_reorder_not_yet_reached():
    """重排「尚未輪到」的條目：產 a 後把 char1 從 ['b','c'] 改成 ['c','b']
    → 下一批應反映磁碟新順序（先 c 再 b）。"""
    print("test_reorder_not_yet_reached:")
    dyn = qc.simulate(real_p=["P"], real_1=["a", "b", "c"],
                      real_2=[], real_u=[],
                      edits={1: {"real_1": ["c", "b"]}})
    batches, final, _ = dyn
    assert [b[1] for b in batches] == ["a", "c", "b"], batches
    assert final[1] == [], final[1]
    print(f"  OK — 重排後批序 {[b[1] for b in batches]}")
    print("  PASS\n")


def test_remove_not_yet_reached():
    """刪除「尚未輪到」的條目：產 a 後把 char1 從 ['b','c'] 改成 ['c']
    → b 不再被產，下一批是 c。"""
    print("test_remove_not_yet_reached:")
    dyn = qc.simulate(real_p=["P"], real_1=["a", "b", "c"],
                      real_2=[], real_u=[],
                      edits={1: {"real_1": ["c"]}})
    batches, final, _ = dyn
    assert [b[1] for b in batches] == ["a", "c"], batches
    assert final[1] == [], final[1]
    print(f"  OK — 刪除 'b' 後批序 {[b[1] for b in batches]}")
    print("  PASS\n")


def test_char1_fallback():
    """角色1 fallback（2026-08-23 接上；先前 `fb_1` 寫死 False）。

    要同時成立三件事，缺一就是回歸：
    1. **不會撐大 pair 數**——長度 1 的 fallback 清單只負責 padding，n 仍由最長的
       真實佇列決定。當初不敢接 char1 fallback 就是怕這個，實際擋住它的是
       `decide` 的 `real_nonempty` 閘門（n 只可能從 0 被撐到 1，而 n==0 正好是
       那道閘門接走的情形）。
    2. **不會被 pop**——fallback 不是佇列條目，`todo_character1.md` 收工後仍是空的。
    3. **真實佇列抽乾後接手**——先前那些 pair 的角色1 是空字串。
    """
    print("test_char1_fallback:")

    # 1. 兩筆 prompt × 空 char1 + fallback → 剛好 2 批，角色1 都是 fallback。
    batches, final, _end = qc.simulate(
        real_p=["P1", "P2"], real_1=[], real_2=[], real_u=[],
        fb_1_text="FC1")
    assert batches == [("P1", "FC1", "", ""), ("P2", "FC1", "", "")], batches
    assert final == ([], [], [], []), final
    print(f"  OK 不撐大 pair 數 — {len(batches)} 批，final={final}")

    # 2. 真實佇列全空、只有 char1 fallback → 剛好 1 批（不是無窮）。
    batches, final, _end = qc.simulate(
        real_p=[], real_1=[], real_2=[], real_u=[], fb_1_text="FC1")
    assert batches == [("", "FC1", "", "")], batches
    assert final == ([], [], [], []), final
    print(f"  OK 只有 char1 fallback — 1 批 {batches[0]}")

    # 3. 真實 char1 有 1 筆、prompt 有 3 筆：char1 被 padding 重複到最後一批才
    #    pop，全程用真實條目，fallback 不介入。
    batches, final, _end = qc.simulate(
        real_p=["P1", "P2", "P3"], real_1=["a"], real_2=[], real_u=[],
        fb_1_text="FC1")
    assert [b[1] for b in batches] == ["a", "a", "a"], batches
    assert final[1] == [], final[1]
    print(f"  OK 真實條目優先 — {[b[1] for b in batches]}")

    # 4. mid-run 把 char1 佇列清空 → 之後的 pair 改吃 fallback（先前是空字串）。
    batches, _final, _end = qc.simulate(
        real_p=["P1", "P2", "P3"], real_1=["a", "b", "c"], real_2=[],
        real_u=[], fb_1_text="FC1", edits={1: {"real_1": []}})
    assert [b[1] for b in batches] == ["a", "FC1", "FC1"], batches
    print(f"  OK 抽乾後接手 — {[b[1] for b in batches]}")
    print("  PASS\n")


def test_skip0_reduces_to_can_pop():
    """skip==0 時 should_pop_at 必須完全等同舊 _can_pop。"""
    print("test_skip0_reduces_to_can_pop:")
    # n>=1：實際 pop 路徑只在清單非空（`if remaining:`）時呼叫 _can_pop，所以
    # 等價性只需在 n>=1 成立。n==0 由呼叫端的 `if not lst` 守衛擋掉。
    for n in range(1, 5):
        for is_last in (False, True):
            old = (n > 1) or is_last  # 舊 _can_pop(remaining) with len==n
            new = qc.should_pop_at(n, 0, is_last)
            assert old == new, (n, is_last, old, new)
    print("  OK — should_pop_at(n,0,is_last) == _can_pop")
    print("  PASS\n")


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    # 自帶 runner 一定要能回答「檔案裡到底宣告了幾支」，否則少跑的時候會印出一句
    # 不帶條件的「ALL N …PASSED」，被讀成「全部都過了」。差額可以存在（吃 pytest
    # fixture／parametrize 的測試 standalone 叫不動），不可以不講。
    # 由 `test_self_runners.test_the_runner_knows_how_many_tests_the_file_declares` 守著。
    try:
        _declared = sum(
            1 for _n in _ast.parse(
                _pathlib.Path(__file__).read_text(encoding="utf-8")).body
            if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and _n.name.startswith("test_"))
        _missing = _declared - len(tests)
        if _missing > 0:
            print(f"注意：另有 {_missing} 支測試 standalone 跑不到"
                  "（需要 pytest fixture／parametrize）；完整結果請跑 pytest。")
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    print(f"ALL {len(tests)} TEST GROUPS PASSED")


if __name__ == "__main__":
    _run_all()
