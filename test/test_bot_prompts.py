"""Unit tests for the externalised bot prompts (`_bot_prompts.load_prompt`).

Prompt 字串被抽到版本庫根目錄下的 `bot_prompts/`，開機時載入；缺檔／壞檔回退到
程式內建 `_DEFAULT_*`。這些測試鎖住四件事：(a) 佔位符替換正確；(b) 缺檔回退到
default；(c) 每個 bot_prompts/ 檔載入後 == 對應的 `_DEFAULT_*`（防止檔案與內建回
退 drift）；(d) `DEFAULT_GENERATE_PROMPT_SUFFIX` 尾端的 ", " 有保住。

No pytest dependency — run directly:
    py -3 test/test_bot_prompts.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _bot_prompts as bp  # noqa: E402
import dorossi_backend as db  # noqa: E402
import discord_bot as b  # noqa: E402


# ---------------------------------------------------------------------------
# tiny harness (mirrors test_bot_helpers.py's style)
# ---------------------------------------------------------------------------
_PASSED = 0


def _group(name, fn):
    global _PASSED
    print(f"{name}:")
    fn()
    print("  PASS\n")
    _PASSED += 1


def _eq(actual, expected, label=""):
    assert actual == expected, f"{label}: got {actual!r}, want {expected!r}"


# The spec that maps each on-disk file → its inline fallback default + the
# sentinel replacements the loader applies. Kept in sync with dorossi_backend /
# discord_bot's wiring; (c) proves file == default for every row.
def _file_specs():
    return [
        ("dorossi_system.md", db._DEFAULT_DOROSSI_SYSTEM_PROMPT, None),
        ("dorossi_loop_verify_guidance.md",
         db._DEFAULT_DOROSSI_LOOP_VERIFY_GUIDANCE, None),
        ("dorossi_loop_tooling_guidance.md",
         db._DEFAULT_DOROSSI_LOOP_TOOLING_GUIDANCE, None),
        ("dorossi_loop_first_suffix.md",
         db._DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX,
         {"sentinel": db.DOROSSI_LOOP_SENTINEL}),
        ("dorossi_loop_continue.md",
         db._DEFAULT_DOROSSI_LOOP_CONTINUE_PROMPT,
         {"sentinel": db.DOROSSI_LOOP_SENTINEL}),
        ("dorossi_loop_pushback.md",
         db._DEFAULT_DOROSSI_LOOP_PUSHBACK_PROMPT,
         {"sentinel": db.DOROSSI_LOOP_SENTINEL}),
        ("dorossi_loop_compact.md",
         db._DEFAULT_DOROSSI_LOOP_COMPACT_PROMPT, None),
        ("dorossi_loop_selfjudge_suffix.md",
         db._DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX,
         {"open_sentinel": db.DOROSSI_LOOP_OPEN_SENTINEL}),
        ("dorossi_injection_preamble.md",
         b._DEFAULT_DOROSSI_INJECTION_PREAMBLE, None),
        ("generate_suffix.txt", b._DEFAULT_GENERATE_PROMPT_SUFFIX, None),
    ]


def test_placeholder_replacement():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "x.md").write_text(
        "頭 {sentinel} 中間 {open_sentinel} 尾（含全形括號）\n",
        encoding="utf-8")
    orig = bp.BOT_PROMPTS_DIR
    try:
        bp.BOT_PROMPTS_DIR = tmp
        got = bp.load_prompt(
            "x.md", "DEFAULT",
            replacements={"sentinel": "S1", "open_sentinel": "O1"})
    finally:
        bp.BOT_PROMPTS_DIR = orig
    # trailing newline stripped, both placeholders substituted, braces content ok
    _eq(got, "頭 S1 中間 O1 尾（含全形括號）", "replacement")
    assert "{sentinel}" not in got and "{open_sentinel}" not in got

    # str.format would choke on the full-width braces / bare {}; replace must not.
    (tmp / "brace.md").write_text("有個 {} 和 {sentinel}\n", encoding="utf-8")
    try:
        bp.BOT_PROMPTS_DIR = tmp
        got2 = bp.load_prompt(
            "brace.md", "DEFAULT", replacements={"sentinel": "ZZZ"})
    finally:
        bp.BOT_PROMPTS_DIR = orig
    _eq(got2, "有個 {} 和 ZZZ", "brace-safe replacement")


def test_missing_file_fallback():
    tmp = Path(tempfile.mkdtemp())  # empty dir → file does not exist
    orig = bp.BOT_PROMPTS_DIR
    try:
        bp.BOT_PROMPTS_DIR = tmp
        got = bp.load_prompt("does_not_exist.md", "FALLBACK-VALUE")
    finally:
        bp.BOT_PROMPTS_DIR = orig
    _eq(got, "FALLBACK-VALUE", "missing-file fallback")


def test_blank_file_fallback():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "blank.md").write_text("   \n\n\t\n", encoding="utf-8")
    orig = bp.BOT_PROMPTS_DIR
    try:
        bp.BOT_PROMPTS_DIR = tmp
        got = bp.load_prompt("blank.md", "FALLBACK-VALUE")
    finally:
        bp.BOT_PROMPTS_DIR = orig
    _eq(got, "FALLBACK-VALUE", "blank-file fallback")


def test_files_match_defaults():
    # (c) 每個 bot_prompts/ 檔載入後（套用哨符替換後）必須逐字 == 對應的內建預設值。
    # 這是防 drift 的核心測試：檔案與 _DEFAULT_* 只要有一個字元不同就會抓到。
    for fname, default, repl in _file_specs():
        got = bp.load_prompt(fname, "SENTINEL_DEFAULT_SHOULD_NOT_APPEAR",
                             replacements=repl)
        _eq(got, default, f"file-vs-default {fname}")
        assert got != "SENTINEL_DEFAULT_SHOULD_NOT_APPEAR", (
            f"{fname} unexpectedly fell back to default (missing/blank?)")
        # No placeholder should survive in a loaded loop prompt.
        assert "{sentinel}" not in got and "{open_sentinel}" not in got


def test_public_constants_are_loaded_values():
    # The public constants downstream imports must equal the loaded file value
    # (which == the default, per test_files_match_defaults).
    _eq(db.DOROSSI_SYSTEM_PROMPT, db._DEFAULT_DOROSSI_SYSTEM_PROMPT, "system")
    _eq(db.DOROSSI_LOOP_FIRST_SUFFIX, db._DEFAULT_DOROSSI_LOOP_FIRST_SUFFIX,
        "first")
    _eq(db.DOROSSI_LOOP_SELFJUDGE_SUFFIX,
        db._DEFAULT_DOROSSI_LOOP_SELFJUDGE_SUFFIX, "selfjudge")
    # sentinels really made it into the loaded prompts
    assert db.DOROSSI_LOOP_SENTINEL in db.DOROSSI_LOOP_FIRST_SUFFIX
    assert db.DOROSSI_LOOP_OPEN_SENTINEL in db.DOROSSI_LOOP_SELFJUDGE_SUFFIX
    # SYSTEM_GUIDANCE stays the concatenation of the two loaded guidances
    _eq(db.DOROSSI_LOOP_SYSTEM_GUIDANCE,
        db.DOROSSI_LOOP_VERIFY_GUIDANCE + db.DOROSSI_LOOP_TOOLING_GUIDANCE,
        "system-guidance concat")


def test_generate_suffix_trailing_space_preserved():
    # (d) 尾端 ", "（含空格）必須逐字保住——接縫處理依賴它；rstrip 只去 \n 不去空白。
    suffix = b.DEFAULT_GENERATE_PROMPT_SUFFIX
    assert suffix.endswith(", "), repr(suffix[-6:])
    assert suffix.startswith(","), repr(suffix[:6])
    _eq(suffix, b._DEFAULT_GENERATE_PROMPT_SUFFIX, "gen-suffix vs default")


def test_never_raises_on_bad_dir():
    # 傳一個「是目錄不是檔」的名字：read_text 會 raise，load_prompt 必須吞掉回 default。
    tmp = Path(tempfile.mkdtemp())
    (tmp / "adir").mkdir()
    orig = bp.BOT_PROMPTS_DIR
    try:
        bp.BOT_PROMPTS_DIR = tmp
        got = bp.load_prompt("adir", "FALLBACK")
    finally:
        bp.BOT_PROMPTS_DIR = orig
    _eq(got, "FALLBACK", "dir-as-file fallback")


# --------------------------------------------------------------------------
# Secrecy Layer 3 的現行版本（2026-08-27 擁有者裁定：全面放寬，只剩憑證值）
# --------------------------------------------------------------------------
# 撤銷掉的人設用過的字眼。整段是「你對 Discord 使用者而言就是一個與本專案無關
# 的獨立通用助理」，外加六類禁止揭露的事項。它已經被裁定取消——這裡列的是那段
# 文字的**指紋**，長回來就紅。
_REVOKED_PERSONA_MARKERS = (
    "獨立、通用的問答助理",
    "獨立通用助理",
    "跟任何專案、系統或基礎設施都毫無關係",
    "不要逐項證實也不要逐項否認",
)

# 唯一保留的那條（理由是不可逆，不是保密立場）必須還在。
_SURVIVING_CREDENTIAL_MARKERS = (
    "不可以送出憑證的內容本身",
    "不可逆",
)


def test_the_system_prompt_does_not_reinstate_the_revoked_persona():
    """撤銷過的規則長回來，症狀是**功能安靜地變少**而不是報錯。

    2026-08-27 擁有者放寬 Secrecy Layer 3：Dorossi 現在可以在任何頻道、對任何
    提問者說明自己是什麼專案、模組結構、外部服務實名、主機路徑與檔名、自己的
    後端如何接線、這一輪改了什麼。實際踩過的落差就是這一條——`CLAUDE.md` 早就
    改成放寬了，但系統提示詞裡那段「獨立通用助理」人設**原封不動**，所以 bot
    當場還是照舊拒答，連擁有者問自己的系統細項都被擋掉。
    規則正本在 `CLAUDE.md` 的 Secrecy Layer 3；要改先改那裡，不是先改提示詞。
    """
    # 兩份都看：檔案是實際送出去的那一份，`_DEFAULT_*` 是缺檔時的回退值——
    # 只長在回退值裡的話，平常看不出來，等到哪天檔案讀不到才整個退化回去。
    for label, prompt in (("檔案", db.DOROSSI_SYSTEM_PROMPT),
                          ("缺檔回退值", db._DEFAULT_DOROSSI_SYSTEM_PROMPT)):
        for marker in _REVOKED_PERSONA_MARKERS:
            assert marker not in prompt, (
                f"系統提示詞（{label}）出現了已撤銷人設的字眼「{marker}」。"
                "Secrecy Layer 3 已於 2026-08-27 全面放寬，"
                "這是在推翻一個刻意的決定。")


def test_the_one_surviving_credential_rule_is_still_there():
    """放寬**不包含**憑證的值，而那一條沒有第二道防線。

    本層放寬的對象是任何頻道的任何人；權杖一旦貼進聊天室就等同帳號被接管，
    刪訊息也救不回來。掉了這句話不會有任何錯誤訊息。
    """
    for label, prompt in (("檔案", db.DOROSSI_SYSTEM_PROMPT),
                          ("缺檔回退值", db._DEFAULT_DOROSSI_SYSTEM_PROMPT)):
        for marker in _SURVIVING_CREDENTIAL_MARKERS:
            assert marker in prompt, (
                f"系統提示詞（{label}）少了憑證界線的關鍵字「{marker}」——"
                "那是 Layer 3 放寬後唯一保留的限制，理由是不可逆。")


def main():
    """自帶 runner：掃 `globals()` 裡的 `test_*` 全部跑一遍。

    **原本是手寫的九行列舉**，於是 2026-09-03 加了 8 支測試之後，自帶 runner 這條路
    照舊只跑那九支、還印「ALL 9 TEST GROUPS PASSED」——一個看起來完全正常的綠燈。
    `test_self_runners` 當時抓不到：它守的是「`if __name__` 要在檔尾」，而列舉式的
    runner 就算 main 區塊擺對位置，漏掉的測試一樣是靜默的。

    改成掃描 ＋ **跑之前先核對數量**（與 `test_webrunner_shared` 同一套做法）：
    數量對不上就當場紅，而不是安靜地少跑。
    """
    import ast as _ast

    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    declared = sum(
        1 for node in _ast.parse(
            Path(__file__).read_text(encoding="utf-8")).body
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and node.name.startswith("test_"))
    if len(tests) != declared:
        raise SystemExit(
            f"self-runner 只看得到 {len(tests)} 支測試，檔案裡卻定義了 {declared} 支。"
            "多半是有人把 `if __name__ == \"__main__\"` 之後又加了測試——那些在"
            "呼叫 `main()` 的當下還沒 bind，會被安靜地略過。把 main 區塊移到檔尾。")
    for name, fn in tests:
        _group(name, fn)
    print(f"ALL {_PASSED} TEST GROUPS PASSED")
    return 0



# ---------------------------------------------------------------------------
# 「這個工作階段跑的是不是磁碟上那份系統提示」
#
# 2026-09-03 補。基底系統提示**只在工作階段的第一輪**送出（`_cc_args` 裡的
# `if not session_id: append_parts.append(DOROSSI_SYSTEM_PROMPT)`）；`--resume`
# 沿用後端記住的那一份。所以編輯 `bot_prompts/dorossi_system.md` 對既有的每一個
# 工作階段完全沒有作用，而且以前沒有任何地方會講。
#
# 代價實際發生過：擁有者 2026-08-27 12:16 改寫這份提示（Layer 3 全面放寬），但當時
# 存在的 7 個工作階段全部建立於那之前，於是放寬**一個都沒生效**，一週後才被發現。
# ---------------------------------------------------------------------------


def test_the_fingerprint_changes_when_the_prompt_changes():
    """指紋要真的跟著內容走，否則整個偵測是裝飾。"""
    a = db.system_prompt_fingerprint("hello")
    b = db.system_prompt_fingerprint("hello ")
    assert a != b, "只差一個空白就該是不同指紋"
    assert a == db.system_prompt_fingerprint("hello"), "同樣的內容要得到同樣的指紋"
    assert len(a) == db.SYSTEM_PROMPT_FINGERPRINT_LEN


def test_the_fingerprint_never_raises_on_junk():
    """這是診斷用的東西，不該把一輪對話弄掛。"""
    for junk in (None, 123, b"bytes", ["list"]):
        if junk is None:
            continue                       # None ＝ 用目前的提示，另外測
        assert db.system_prompt_fingerprint(junk)
    assert db.system_prompt_fingerprint()   # 預設參數走目前的提示


def test_a_session_created_with_the_current_prompt_is_current():
    fp = db.system_prompt_fingerprint()
    assert db.session_prompt_state({"sys_prompt_fp": fp}) == "current"


def test_a_session_created_with_an_older_prompt_is_stale():
    assert db.session_prompt_state({"sys_prompt_fp": "0" * 16}) == "stale"


def test_a_legacy_session_without_a_fingerprint_is_unknown_not_stale():
    """`unknown` 不可以被當成 `stale`。

    指紋是 2026-09-03 才開始記的，在那之前建立的工作階段一個欄位都沒有。把它們
    全部報成過期＝每個舊工作階段都亮紅燈，而會亂叫的提示最後會被人忽略——
    `test_language` 的字表就是為了同一個理由刻意留白的。
    """
    for legacy in ({}, {"sys_prompt_fp": ""}, {"sys_prompt_fp": None},
                   {"sys_prompt_fp": 123}, "not a dict", None):
        assert db.session_prompt_state(legacy) == "unknown", legacy


def test_the_fingerprint_is_recorded_exactly_when_a_session_is_created():
    """必須綁在 `cc_session_id` 的 None → id 轉換上。

    那一刻正是後端帶了基底系統提示的那一輪（`_cc_args` 只在 `not session_id` 時
    附上）。綁錯地方會讓指紋每輪被覆寫成「目前的」，於是永遠 `current`、永遠不會
    報過期——一個看起來完全正常、實際上什麼都不做的偵測。

    用 AST 檢查那個 `if not sess.get("cc_session_id")` 的守衛真的在。
    """
    import ast
    tree = ast.parse((Path(db.__file__).parent / "discord_bot.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_dorossi_persist_advance")
    writes = [n for n in ast.walk(fn)
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Subscript)
                      and isinstance(t.slice, ast.Constant)
                      and t.slice.value == "sys_prompt_fp" for t in n.targets)]
    assert writes, "`_dorossi_persist_advance` 沒有記錄 sys_prompt_fp"
    guarded = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        src = ast.unparse(node.test)
        if "cc_session_id" in src and src.strip().startswith("not "):
            guarded.extend(w for w in writes
                           if any(w is s for s in ast.walk(node)))
    assert guarded, (
        "sys_prompt_fp 的寫入沒有被 `if not sess.get(\"cc_session_id\")` 包住——"
        "那會讓每一輪都覆寫成目前的指紋，偵測永遠不會報過期。")


def test_the_session_list_warns_about_a_stale_prompt():
    """偵測寫好卻沒人顯示，就只是一段沒人跑的程式碼。"""
    import ast
    tree = ast.parse((Path(db.__file__).parent / "discord_bot.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_render_session_list")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_dorossi_session_prompt_state" in called, (
        "`/dorossi session list` 沒有檢查系統提示是不是舊版的。")


def test_the_stale_warning_does_not_leak_a_file_name():
    """Layer 1：對外訊息不得出現檔名／路徑。"""
    import ast
    tree = ast.parse((Path(db.__file__).parent / "discord_bot.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dorossi_render_session_list")
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "舊版設定" in node.value:
                for banned in (".md", "dorossi_system", "bot_prompts", "/"):
                    assert banned not in node.value, (
                        f"過期提示的警告字串帶了 {banned!r}：{node.value!r}")


# ---------------------------------------------------------------------------
# 哨符的**另一半**：剝除
#
# 上面幾支守的是「提示詞有沒有把哨符教給後端」。這一段守的是接收端——
# `_dorossi_strip_loop_sentinel` / `_dorossi_strip_open_sentinel` /
# `_dorossi_redact_sentinel_stream`。2026-09-05 量覆蓋率時發現這三支**一支測試都
# 沒有**，而它們決定的是兩件事：自走迴圈要不要停，以及內部控制字串會不會出現在
# 使用者看得到的訊息裡（串流預覽是逐塊重繪的，半截哨符會一閃而過）。
#
# 順手修掉一個真的缺陷：三支原本都用單次 `str.replace`。移除會把左右兩邊接起來，
# 所以夾心寫法（`<<<DOROSSI-LOOP-` ＋ 完整哨符 ＋ `DONE>>>`）在移掉內層之後會**重新
# 拼出**一個完整哨符，單次 replace 的輸出因此仍然含有哨符——而這三支的整個存在
# 理由就是「送出去之前一個都不能剩」。現在共用 `_dorossi_remove_all_sentinels`，
# 掃到不再變動為止。
# ---------------------------------------------------------------------------

_DONE = db.DOROSSI_LOOP_SENTINEL
_OPEN = db.DOROSSI_LOOP_OPEN_SENTINEL


def test_the_loop_sentinel_is_detected_and_stripped():
    cleaned, done = db._dorossi_strip_loop_sentinel(
        "這一輪做完了。\n" + _DONE)
    assert done is True
    _eq(cleaned, "這一輪做完了。", "哨符與前後空白都要清掉")
    assert _DONE not in cleaned


def test_an_answer_without_the_loop_sentinel_is_untouched():
    """沒有哨符時必須原封不動回傳——連 `strip()` 都不做。

    這條路是每一輪都會走的；順手 strip 會把後端刻意排版的前後空白吃掉。
    """
    text = "  還有事情可以做，繼續。  "
    cleaned, done = db._dorossi_strip_loop_sentinel(text)
    assert done is False
    _eq(cleaned, text, "沒有哨符就不該動到內容")


def test_an_answer_that_is_only_the_sentinel_becomes_empty():
    """呼叫端用「剝完還有沒有正文」決定要不要貼訊息，所以空字串是有意義的回傳值。"""
    cleaned, done = db._dorossi_strip_loop_sentinel(_DONE)
    assert done is True
    _eq(cleaned, "", "只有哨符 → 沒有正文")


def test_a_sandwiched_sentinel_does_not_survive_removal():
    """夾心寫法：移掉內層之後左右接起來又是一個完整哨符。

    單次 `str.replace` 會把這個放出去。這正是「單次替換不是清洗」那一類錯誤。
    """
    head, tail = _DONE[:16], _DONE[16:]
    cleaned, done = db._dorossi_strip_loop_sentinel(head + _DONE + tail)
    assert done is True
    assert _DONE not in cleaned, (
        "剝完之後還剩一個完整哨符：" + repr(cleaned))


def test_the_open_sentinel_gets_the_same_treatment():
    head, tail = _OPEN[:16], _OPEN[16:]
    cleaned, opened = db._dorossi_strip_open_sentinel(
        "先講一下計畫。\n" + head + _OPEN + tail)
    assert opened is True
    assert _OPEN not in cleaned, (
        "剝完之後還剩一個完整的開場哨符：" + repr(cleaned))


def test_an_answer_without_the_open_sentinel_is_untouched():
    text = "  這題一輪就答得完。  "
    cleaned, opened = db._dorossi_strip_open_sentinel(text)
    assert opened is False
    _eq(cleaned, text)


def test_the_two_sentinels_do_not_strip_each_other():
    """完成哨符的剝除不該動到開場哨符，反之亦然——兩者共用長前綴，很容易寫錯。"""
    cleaned, done = db._dorossi_strip_loop_sentinel(_OPEN + "正文" + _DONE)
    assert done is True
    assert _OPEN in cleaned, "剝完成哨符時把開場哨符也吃掉了"
    cleaned2, opened = db._dorossi_strip_open_sentinel(_OPEN + "正文" + _DONE)
    assert opened is True
    assert _DONE in cleaned2, "剝開場哨符時把完成哨符也吃掉了"


def test_the_stream_preview_hides_a_complete_sentinel():
    for sentinel in (_DONE, _OPEN):
        got = db._dorossi_redact_sentinel_stream("進度說明\n" + sentinel + "\n後面")
        assert sentinel not in got, sentinel


def test_the_stream_preview_hides_every_half_streamed_prefix():
    """半截哨符也要藏起來——串流是逐塊重繪的，一閃而過的人看得到。

    對兩個哨符的**每一個**前綴長度都驗一次，而不是挑一個代表值：兩者共用
    `<<<DOROSSI-LOOP-` 這段長前綴，只有分岔之後的那幾個長度才分得出實作有沒有寫對。
    """
    for sentinel in (_DONE, _OPEN):
        for i in range(1, len(sentinel)):
            got = db._dorossi_redact_sentinel_stream("正文" + sentinel[:i])
            _eq(got, "正文",
                "半截哨符沒藏起來（" + sentinel[:i] + "）")


def test_the_stream_preview_leaves_ordinary_text_alone():
    for text in ("普通的一段話", "帶 < 與 <<< 的文字在中間 < 也沒關係",
                 "結尾是句號。", ""):
        _eq(db._dorossi_redact_sentinel_stream(text), text, repr(text))


def test_the_stream_preview_only_trims_the_tail():
    """夾在中間的 `<<<` 不是半截哨符，不能砍。"""
    text = "看這個 <<< 符號，後面還有字"
    _eq(db._dorossi_redact_sentinel_stream(text), text)


def test_no_split_of_a_streamed_answer_ever_shows_a_sentinel():
    """最強的一支：把整段答案逐字元餵進去，模擬真正的串流重繪。

    後端是一小塊一小塊吐字的，而預覽每收到一塊就重繪一次。所以「哨符不會外洩」
    要對**每一個切點**都成立，不是只對完整的那一份成立。挑一個切點來測會漏掉
    真正會出事的那幾個。
    """
    answer = "先做了 A，再做 B。\n" + _DONE + "\n收尾說明" + _OPEN
    for i in range(len(answer) + 1):
        shown = db._dorossi_redact_sentinel_stream(answer[:i])
        for sentinel in (_DONE, _OPEN):
            assert sentinel not in shown, (
                "切在第 " + str(i) + " 個字元時預覽出現了哨符：" + repr(shown))


def test_the_stream_preview_survives_a_sandwiched_sentinel():
    head, tail = _DONE[:16], _DONE[16:]
    got = db._dorossi_redact_sentinel_stream("正文" + head + _DONE + tail)
    assert _DONE not in got, repr(got)


if __name__ == "__main__":
    sys.exit(main())
