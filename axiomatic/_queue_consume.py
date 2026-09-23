"""佇列消耗決策（純函式，無 Selenium / 無 I/O）。

兩支 webrunner 的 `main()` 由「啟動當下一次讀完四個 todo 佇列、整輪跑這份快照」
改成「每產完一個角色就重讀磁碟、取佇列最前面一批生成」，讓 run 進行中對「還沒
輪到的」佇列條目所做的新增 / 重排 / 刪除，這一輪就生效。

把每一圈的「該不該繼續、該產哪一批、產完該怎麼 pop」抽成純函式放這裡，理由：

1. **可測**：不需要 Chrome 就能對各種佇列形狀做等價性 / 新行為測試
   （見 test_dynamic_consume.py）。
2. **兩支 webrunner 共用同一份決策**：避免 selenium 版與 je 版各寫一份、日後
   失同步。`main()` 只負責 I/O（讀寫磁碟、reconcile、generate、log），決策一律
   呼叫這裡。

被 `webrunner_novelai.py` / `webrunner_je_only.py` 匯入；屬於 CLAUDE.md 允許的
被動共用模組（不是 bot↔webrunner 直接 import）。

術語：
- `real_*`：fallback 「之前」的真實佇列（從 todo_*.md 讀出來的）。空 = 該佇列
  已抽乾。
- `eff_*`：套用 fallback 替代「之後」的有效清單（空 prompt 沿用 prompt.md…），
  實際拿去配對 / 生成的就是這份。**四個清單都有 fallback**（prompt.md /
  character1.md / character2.md / undesired.md）。
- `fb_*`：該清單目前是否為 fallback（True 表示 eff 來自 .md 預設、real 為空）。
- `skip`：本輪「未達門檻、保留在佇列前端、已嘗試過」的條目數，是個游標；
  下一批取 `pairs[skip]`。
- `produced`：本輪已實際產出的角色數（driver chrome 重啟 cadence / log）。
"""

from __future__ import annotations

from typing import NamedTuple


def pair_todos(*todos: list[str]) -> list[tuple[str, ...]]:
    """與 webrunner 的 pair_todos 同義（純函式，這裡留一份給測試 / 決策共用）。
    短清單重複最後一筆做 padding；空清單變成空字串；全空回 []。"""
    if not any(todos):
        return []
    n = max(len(t) for t in todos)

    def fill(lst: list[str]) -> list[str]:
        if not lst:
            return [""] * n
        return lst + [lst[-1]] * (n - len(lst))

    return list(zip(*(fill(t) for t in todos)))


# `end` sentinel：todo_prompt 佇列裡（strip().lower() 後）等於 "end" 的一行是停止
# marker。webrunner 的 run_batch 走到那個 pair 時跳過該 pair 與其後全部、乾淨收工
# （rc=0）；bot 的 !plan / !eta 預覽也用它截斷 pair 數。常數＋helper 放這裡當「消費
# 語彙」的單一來源：webrunner（_webrunner_shared）與 bot（discord_bot）都引用，sentinel
# 一旦要改只改這裡——simulate / reference_snapshot 的 end_token 預設已直接引用
# END_SENTINEL，不再各自寫死字面值。
END_SENTINEL = "end"


def is_end_marker(entry: str) -> bool:
    """該 todo_prompt 條目是否為 `end` 停止 marker（strip 後不分大小寫比對）。"""
    return entry.strip().lower() == END_SENTINEL


# 決策動作（step 2 終止判斷的三種結果）。
ACTION_BREAK = "break"            # 沒有真實工作（也沒有 fallback 單發要做）→ 收工
ACTION_GENERATE = "generate"      # 有真實工作，產 pairs[skip]
ACTION_FALLBACK_SINGLE = "fallback_single"  # 真實佇列全空、但有 fallback，且本輪
#                                              還沒產過任何角色 → 只產這一個就收工


class Decision(NamedTuple):
    """一圈的決策結果。

    - action：ACTION_BREAK / ACTION_GENERATE / ACTION_FALLBACK_SINGLE。
    - batch：action 非 break 時為 `(prompt, char1, char2, undesired)`，break 時 None。
    - pairs：本圈以當前磁碟內容重算的 pair_todos（pop 的 is_last 用得到）；
      break 時為當下算出的（可能空）清單。
    - eff：(eff_p, eff_1, eff_2, eff_u)（fallback 替代後）。
    - fb：(fb_p, fb_1, fb_2, fb_u)。
    """
    action: str
    batch: tuple[str, str, str, str] | None
    pairs: list[tuple[str, ...]]
    eff: tuple[list[str], list[str], list[str], list[str]]
    fb: tuple[bool, bool, bool, bool]


def decide(
    real_p: list[str],
    real_1: list[str],
    real_2: list[str],
    real_u: list[str],
    eff_p: list[str],
    eff_1: list[str],
    eff_2: list[str],
    eff_u: list[str],
    fb_p: bool,
    fb_1: bool,
    fb_2: bool,
    fb_u: bool,
    skip: int,
    produced: int,
) -> Decision:
    """一圈的終止 / 取批決策（計畫 step 1-3）。

    呼叫端負責「讀真實佇列 → 套 fallback → 算出 eff / fb」這段 I/O，再把結果丟進
    來。本函式不碰磁碟。

    終止順序（嚴格照此）：
      A. real_nonempty = 任一真實佇列（fallback 前）非空。
      B. not real_nonempty：
         - produced == 0 且有任何 fallback 作用中 → 只產一個 fallback 角色
           （ACTION_FALLBACK_SINGLE）；
         - 否則 ACTION_BREAK。
         理由：pair_todos 的 n = max(len) 由最長佇列驅動；fallback 清單長度 1 只
         負責 padding，不能把已抽乾的真實佇列延長成多餘的幽靈角色。**這道
         `real_nonempty` 閘門就是唯一擋住幽靈角色的東西**——長度 1 的 fallback
         清單只可能把 n 從 0 撐到 1，而 n==0 的情形正是被這裡接走的。char1 的
         fallback 2026-08-23 才接上（先前 `fb_1` 寫死 False），靠的就是這個事實。
      C. real_nonempty：pairs = pair_todos(*eff)；skip >= len(pairs) → ACTION_BREAK
         （剩下的都是已嘗試、保留待下輪）。否則 ACTION_GENERATE，batch = pairs[skip]。
    """
    fb = (fb_p, fb_1, fb_2, fb_u)
    eff = (list(eff_p), list(eff_1), list(eff_2), list(eff_u))
    real_nonempty = any((real_p, real_1, real_2, real_u))

    if not real_nonempty:
        any_fallback = fb_p or fb_1 or fb_2 or fb_u
        if produced == 0 and any_fallback:
            pairs = pair_todos(*eff)
            # 真實佇列全空時 eff 仍可能算出 >1 批（例如某 fallback 清單長度 > 1，
            # 但那不該發生：fallback 一律是單筆 .md）。保險取 pairs[0]。
            if not pairs:
                return Decision(ACTION_BREAK, None, pairs, eff, fb)
            batch = pairs[0]
            return Decision(ACTION_FALLBACK_SINGLE, batch, pairs, eff, fb)
        return Decision(ACTION_BREAK, None, [], eff, fb)

    pairs = pair_todos(*eff)
    if skip >= len(pairs):
        return Decision(ACTION_BREAK, None, pairs, eff, fb)
    batch = pairs[skip]
    return Decision(ACTION_GENERATE, batch, pairs, eff, fb)


def should_pop_at(remaining_len: int, skip: int, is_last: bool) -> bool:
    """游標位置 `ri = min(skip, remaining_len - 1)` 那筆該不該 pop。

    沿用既有 `_can_pop`：該清單剩餘長度 > 1（游標位置不是被 padding 重複的最後
    一筆）或 is_last（最後一批，沒有後面的 pair 還要重用這條尾巴）才 pop。

    skip == 0 時 `ri = 0`、`remaining_len > 1 or is_last` 完全等同舊版 front-pop
    的 `_can_pop`。
    """
    if remaining_len <= 0:
        return False
    ri = min(skip, remaining_len - 1)
    # ri 位置不是被 padding 重複的尾巴（ri < len-1），或這是最後一批。
    return ri < remaining_len - 1 or is_last


def pop_index(remaining_len: int, skip: int) -> int:
    """游標對應的索引：min(skip, len-1)（短清單被 padding 時夾在最後一筆）。"""
    return min(skip, remaining_len - 1)


# ---------------------------------------------------------------------------
# 純模擬器：只給測試用。把 main() 的「逐批重讀 + skip 游標 + fallback 單發終止 +
# 門檻 pop」對 in-memory 清單跑一遍（無磁碟 I/O、無 Selenium）。回傳產出的批序與
# 最終各佇列狀態，讓 test_dynamic_consume.py 對「快照參考模型」做等價性斷言、對
# 「mid-run 編輯」做新行為斷言。
# ---------------------------------------------------------------------------


def simulate(
    real_p: list[str],
    real_1: list[str],
    real_2: list[str],
    real_u: list[str],
    fb_p_text: str | None = None,
    fb_1_text: str | None = None,
    fb_2_text: str | None = None,
    fb_u_text: str | None = None,
    fail_set: set[int] | None = None,
    edits: dict[int, dict] | None = None,
    end_token: str = END_SENTINEL,
):
    """純模擬動態消耗。

    參數：
    - real_*：起始磁碟佇列（會被當作可變狀態逐批 pop）。
    - fb_*_text：對應 fallback 文字（None 表示沒有 fallback；空佇列且有 fallback
      時 eff = [fb_text]、fb 旗標 True）。四個清單都有。
    - fail_set：「批序號（從 0 起算，第幾個產出的角色）」集合 → 該批模擬未達門檻
      （不 pop、skip += 1）。
    - edits：{在「第 k 批產出之前」套用: {"real_1": newlist, ...}}，模擬 mid-run
      對磁碟佇列的新增 / 重排 / 刪除。key 是 produced 計數（即「即將產第 k 個角色
      前」）。值是要覆寫的真實佇列（任一 real_p/1/2/u）。
    - end_token：end sentinel 字串（比對前會 strip().lower()）。

    回傳 (batches, final_lists, stopped_by_end)：
    - batches：list of (prompt, char1, char2, undesired)，依序產出的批。
    - final_lists：(real_p, real_1, real_2, real_u) 收工後的磁碟狀態。
    - stopped_by_end：是否因 end sentinel 收工。
    """
    fail_set = fail_set or set()
    edits = edits or {}
    # 用可變 list 模擬磁碟。
    p, c1, c2, u = list(real_p), list(real_1), list(real_2), list(real_u)

    batches: list[tuple[str, str, str, str]] = []
    produced = 0
    skip = 0
    stopped_by_end = False

    # 防呆：避免無窮迴圈。每圈不是 produced+1 就是 skip+1 或 break，所以上限取
    # 一個寬鬆值。
    guard = 0
    guard_max = (len(real_p) + len(real_1) + len(real_2) + len(real_u)
                 + 16) * 4 + 64

    while True:
        guard += 1
        if guard > guard_max:
            raise RuntimeError("simulate: 迴圈未收斂（決策邏輯有 bug）")

        # mid-run 編輯：在「即將產第 produced 個角色之前」套用。
        if produced in edits:
            ed = edits.pop(produced)
            if "real_p" in ed:
                p = list(ed["real_p"])
            if "real_1" in ed:
                c1 = list(ed["real_1"])
            if "real_2" in ed:
                c2 = list(ed["real_2"])
            if "real_u" in ed:
                u = list(ed["real_u"])

        # step 1：套 fallback → eff + fb。
        eff_p, fb_p = (list(p), False)
        if not p and fb_p_text is not None:
            eff_p, fb_p = [fb_p_text], True
        eff_1, fb_1 = (list(c1), False)
        if not c1 and fb_1_text is not None:
            eff_1, fb_1 = [fb_1_text], True
        eff_2, fb_2 = (list(c2), False)
        if not c2 and fb_2_text is not None:
            eff_2, fb_2 = [fb_2_text], True
        eff_u, fb_u = (list(u), False)
        if not u and fb_u_text is not None:
            eff_u, fb_u = [fb_u_text], True

        dec = decide(p, c1, c2, u, eff_p, eff_1, eff_2, eff_u,
                     fb_p, fb_1, fb_2, fb_u, skip, produced)

        if dec.action == ACTION_BREAK:
            break

        prompt_entry, entry1, entry2, undesired_entry = dec.batch
        fallback_single = dec.action == ACTION_FALLBACK_SINGLE

        # step 4：end sentinel（只比對 prompt 欄、且非 fallback）。
        if not fb_p and prompt_entry.strip().lower() == end_token:
            # 從真實 p 移掉第一個 end 行。
            for idx, rp in enumerate(p):
                if rp.strip().lower() == end_token:
                    p.pop(idx)
                    break
            stopped_by_end = True
            break

        batches.append((prompt_entry, entry1, entry2, undesired_entry))
        this_batch_no = produced
        produced += 1

        # step 6/7：依門檻決定 pop / 保留。
        if this_batch_no in fail_set:
            # 未達門檻 → 不 pop，保留在前端、游標越過。
            skip += 1
            continue

        pairs = dec.pairs
        is_last = (len(pairs) - skip) <= 1

        # 對每個非 fallback 清單，pop 游標位置那筆（含 front-match 守衛）。
        def _pop(lst, entry, is_fb, reuse_tail=True):
            if is_fb:
                return
            if not lst:
                return
            ri = pop_index(len(lst), skip)
            if reuse_tail and not should_pop_at(len(lst), skip, is_last):
                return
            # front-match 守衛：游標位置那筆要等於剛消耗的 entry 才 pop。
            if lst[ri] != entry:
                return
            lst.pop(ri)

        _pop(p, prompt_entry, fb_p)
        _pop(c1, entry1, fb_1)
        # Real Character 2 entries are one-shot. Once the queue is exhausted,
        # later Character 1 batches must get an empty char2 (or the configured
        # fallback), never a repeated stale final entry.
        _pop(c2, entry2, fb_2, reuse_tail=False)
        _pop(u, undesired_entry, fb_u)

        if fallback_single:
            break

    return (batches, (p, c1, c2, u), stopped_by_end)


def reference_snapshot(
    real_p: list[str],
    real_1: list[str],
    real_2: list[str],
    real_u: list[str],
    fb_p_text: str | None = None,
    fb_1_text: str | None = None,
    fb_2_text: str | None = None,
    fb_u_text: str | None = None,
    end_token: str = END_SENTINEL,
):
    """舊「啟動快照」參考模型：一次 pair_todos(*eff)、逐筆 _can_pop front-pop。
    給等價性測試當 ground truth（沒有 mid-run 編輯、沒有 below-threshold 時，動態
    模型必須與這份逐筆相同）。"""
    eff_p, fb_p = (list(real_p), False)
    if not real_p and fb_p_text is not None:
        eff_p, fb_p = [fb_p_text], True
    eff_1, fb_1 = (list(real_1), False)
    if not real_1 and fb_1_text is not None:
        eff_1, fb_1 = [fb_1_text], True
    eff_2, fb_2 = (list(real_2), False)
    if not real_2 and fb_2_text is not None:
        eff_2, fb_2 = [fb_2_text], True
    eff_u, fb_u = (list(real_u), False)
    if not real_u and fb_u_text is not None:
        eff_u, fb_u = [fb_u_text], True

    n = max(len(eff_p), len(eff_1), len(eff_2), len(eff_u))

    def _repeat_tail(lst):
        if not lst:
            return [""] * n
        return lst + [lst[-1]] * (n - len(lst))

    # Real char2 entries are consumed once each. A char2 fallback remains a
    # persistent default, but a short real queue is blank after it runs out.
    if real_2:
        snap_2 = list(real_2) + [""] * (n - len(real_2))
    else:
        snap_2 = _repeat_tail(eff_2)
    pairs = list(zip(_repeat_tail(eff_p), _repeat_tail(eff_1),
                     snap_2, _repeat_tail(eff_u)))

    rem_p, rem_1, rem_2, rem_u = (list(real_p), list(real_1),
                                  list(real_2), list(real_u))
    batches: list[tuple[str, str, str, str]] = []
    stopped_by_end = False

    for pair_idx, (pe, e1, e2, eu) in enumerate(pairs):
        if not fb_p and pe.strip().lower() == end_token:
            for idx, rp in enumerate(rem_p):
                if rp.strip().lower() == end_token:
                    rem_p.pop(idx)
                    break
            stopped_by_end = True
            break
        batches.append((pe, e1, e2, eu))
        is_last_pair = pair_idx == len(pairs) - 1

        def _can_pop(remaining):
            return len(remaining) > 1 or is_last_pair

        if not fb_p and rem_p and pe and rem_p[0] == pe and _can_pop(rem_p):
            rem_p.pop(0)
        if not fb_1 and rem_1 and e1 and rem_1[0] == e1 and _can_pop(rem_1):
            rem_1.pop(0)
        if not fb_2 and rem_2 and e2 and rem_2[0] == e2:
            rem_2.pop(0)
        if not fb_u and rem_u and rem_u[0] == eu and _can_pop(rem_u):
            rem_u.pop(0)

    return (batches, (rem_p, rem_1, rem_2, rem_u), stopped_by_end)
