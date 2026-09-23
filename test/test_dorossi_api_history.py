"""`api` 後端重送的對話歷史必須有界限——`_dorossi_trim_api_history`。

三個後端只有 `api` 這一條會踩到這件事，因為只有它是**無狀態**的：每一輪都要把整份
對話歷史再送一次。`claude_code` 的歷史在後端（`--resume` 帶 session id），另有
`/compact` 與自走迴圈的輪數／花費觸發；`codex` 的脈絡同樣在後端，也不由我們攜帶。

沒有界限的話，三個後果依嚴重度排：

1. 輸入 token 隨輪數線性成長，**總成本是輪數的平方**；
2. 長到超過脈絡窗之後會拿到 400（`invalid_request_error`）。**那是 4xx，不是暫時性
   錯誤**——暫時性判定會正確地說「不是暫時性」，`dorossi_error_is_fatal` 也不認得
   它，於是自走迴圈用同一份過長的歷史重試到放棄，然後**每一輪都以完全相同的方式
   失敗**。沒有任何自我修復的路徑，除非有人知道要下 `/new`；
3. `dorossi_session.json` 每輪整檔重寫，檔案跟著長。

第 2 條是這份測試真正在防的東西：它不是「偶爾出錯」，是「一旦跨過就永久壞掉」。

修剪的兩個決定各有反例測試：**切齊到 user 開頭**（Messages API 不接受以 assistant
開頭的 `messages`，而從中間切下去有一半機率正好切在 assistant 上），以及**修剪時要
出聲**（寫 stderr，不寫 Discord——Layer 1）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _bot_config as bc  # noqa: E402
import dorossi_backend as db  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


def _history(n: int, *, start: str = "user") -> list[dict]:
    """n 則一問一答的歷史，`start` 決定第一則的角色。"""
    roles = ("user", "assistant") if start == "user" else ("assistant", "user")
    return [{"role": roles[i % 2], "content": f"m{i}"} for i in range(n)]


# --------------------------------------------------------------------------
# 界限本身
# --------------------------------------------------------------------------

def test_a_long_history_is_cut_down_to_the_cap():
    kept = db._dorossi_trim_api_history(_history(500), 40)
    assert len(kept) <= 40


def test_the_cut_keeps_the_most_recent_turns_not_the_oldest():
    """滑動視窗要往回看。留最舊的等於每一輪都在回答一個早就結束的話題。"""
    kept = db._dorossi_trim_api_history(_history(500), 40)
    assert kept[-1]["content"] == "m499"
    assert all(row["content"] != "m0" for row in kept)


def test_a_short_history_is_returned_untouched():
    rows = _history(6)
    assert db._dorossi_trim_api_history(rows, 40) == rows


def test_a_history_exactly_at_the_cap_is_not_cut():
    """邊界：剛好等於上限不算超過。差一格的錯誤在這裡是「白白丟掉一輪脈絡」。"""
    rows = _history(40)
    assert db._dorossi_trim_api_history(rows, 40) == rows


@pytest.mark.parametrize("cap", [0, -1])
def test_zero_or_negative_means_no_limit(cap):
    """0 ＝停用，與 `dorossi_max_budget_usd` 同慣例。負數同樣不該變成「全部丟掉」。"""
    rows = _history(500)
    assert db._dorossi_trim_api_history(rows, cap) == rows


# --------------------------------------------------------------------------
# 切齊到 user 開頭
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cap", [39, 40, 41, 7, 8])
def test_the_kept_history_always_starts_with_a_user_message(cap):
    """Messages API 不接受以 assistant 開頭的 `messages`。

    從中間切下去有一半的機率正好切在 assistant 上，所以這不是整潔問題——切錯的
    那一半會讓每一輪都拿到 400，而 400 正是這整條界限要避免的東西。**用一個修好
    之前會壞的形狀來測**：cap 為奇數時，`rows[-cap:]` 必然落在 assistant 上。
    """
    kept = db._dorossi_trim_api_history(_history(500), cap)
    assert kept, "全部被丟掉了"
    assert kept[0]["role"] == "user", (
        f"cap={cap} 切出來的第一則是 {kept[0]['role']}——API 會回 400")


def test_aligning_never_pushes_the_result_over_the_cap():
    """切齊只能往後丟一則，不能往前多留一則——多留就是這個界限有時候不成立。"""
    for cap in range(2, 60):
        kept = db._dorossi_trim_api_history(_history(500), cap)
        assert len(kept) <= cap, f"cap={cap} 卻留了 {len(kept)} 則"


def test_a_history_that_starts_with_an_assistant_still_ends_up_valid():
    """歷史本身就以 assistant 開頭（界限上線前存下來的那些）也要救得回來。

    **這一支抓到的是實作的第一版**：當時切齊寫在「有超過上限」那條分支裡，所以一份
    只有十則、但開頭是 assistant 的歷史會被原封不動送出去，照樣 400。修剪與切齊是
    兩件事——前者有條件，後者沒有。
    """
    kept = db._dorossi_trim_api_history(_history(10, start="assistant"), 40)
    assert kept[0]["role"] == "user"


def test_rows_that_are_not_dicts_are_dropped():
    """`dorossi_session.json` 是磁碟上的檔案，壞掉的列不該讓整輪炸在 `.get` 上。"""
    rows = [1, "x", None, {"role": "user", "content": "ok"}]
    assert db._dorossi_trim_api_history(rows, 10) == [
        {"role": "user", "content": "ok"}]


# --------------------------------------------------------------------------
# 修剪要出聲，但只對 stderr 出聲
# --------------------------------------------------------------------------

def test_trimming_leaves_a_line_on_stderr(capsys):
    """「答案為什麼忘了前面講過的事」總有一天有人要查，查的時候要有東西可看。"""
    db._dorossi_trim_api_history(_history(500), 40)
    err = capsys.readouterr().err
    assert "trim" in err.lower(), f"修剪沒有留下任何紀錄：{err!r}"


def test_not_trimming_says_nothing(capsys):
    """沒修剪就不要吵。每一輪都印一行沒事的訊息，下場是沒人再看它。"""
    db._dorossi_trim_api_history(_history(6), 40)
    assert capsys.readouterr().err == ""


def test_the_trim_notice_never_reaches_the_chat_platform():
    """Layer 1：這條訊息只能進 stderr。

    修剪的紀錄裡帶著則數與上限，本身無害；但把「內部狀態」寫進送出路徑是滑坡的
    第一步，而這支函式沒有提問者可以判定，連擁有者例外都用不上。所以在原始碼上
    釘死：它只准 `print(..., file=sys.stderr)`。
    """
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_dorossi_trim_api_history"), None)
    assert fn is not None, "函式改名了——這支守門要跟著改"
    prints = [c for c in ast.walk(fn)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "print"]
    assert prints, "這支不再說任何話了——修剪必須留得下紀錄"
    for call in prints:
        assert any(kw.arg == "file" and ast.unparse(kw.value) == "sys.stderr"
                   for kw in call.keywords), (
            f"{ast.unparse(call)[:80]} 沒有指定 `file=sys.stderr`")

    # 送出路徑一個都不准出現。這是白名單的反面：不列舉「哪些算 print」，而是
    # 列舉「哪些算送出」，因為前者會隨著有人加一個 helper 而失效。
    banned = ("reply", "send", "safe_reply", "followup", "edit")
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        name = (call.func.attr if isinstance(call.func, ast.Attribute)
                else getattr(call.func, "id", ""))
        assert name not in banned, (
            f"{ast.unparse(call)[:80]}：這支只能對 stderr 說話（Layer 1）")


# --------------------------------------------------------------------------
# 接線：設定鍵存在、預設值合理、而且真的被用在送出路徑上
# --------------------------------------------------------------------------

def test_the_cap_is_a_config_key_with_a_sane_default():
    cfg = bc.load_bot_config()
    assert "dorossi_api_history_max_msgs" in cfg, (
        "`load_bot_config` 沒有列舉這個鍵——`dorossi_backend` 在模組層直接下標它，"
        "少一個鍵就是 import 期 KeyError，也就是 bot 起不來")
    value = cfg["dorossi_api_history_max_msgs"]
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value >= 2, "預設值小於一問一答，等於每一輪都失憶"


@pytest.mark.parametrize("bad", ["40", 1.5, None, True, -3, float("inf")])
def test_a_bad_value_in_the_file_falls_back_to_the_default(bad, tmp_path,
                                                           monkeypatch):
    """設定檔是使用者手改的，型別檢查就是全部的防線。"""
    import json
    path = tmp_path / "bot_config.json"
    path.write_text(json.dumps({"dorossi_api_history_max_msgs": bad}),
                    encoding="utf-8")
    monkeypatch.setattr(bc, "BOT_CONFIG_FILE", path)
    got = bc.load_bot_config()["dorossi_api_history_max_msgs"]
    assert got == bc._DEFAULT_BOT_CONFIG["dorossi_api_history_max_msgs"]


def test_zero_survives_the_coercer():
    """0 是「停用」這個合法意思，不能被當成壞值換掉。"""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_config.json"
        path.write_text(json.dumps({"dorossi_api_history_max_msgs": 0}),
                        encoding="utf-8")
        real = bc.BOT_CONFIG_FILE
        try:
            bc.BOT_CONFIG_FILE = path
            assert bc.load_bot_config()["dorossi_api_history_max_msgs"] == 0
        finally:
            bc.BOT_CONFIG_FILE = real


def test_the_send_path_actually_trims():
    """`_dorossi_via_api` 必須在組 `msgs` 的時候就過修剪。

    只有函式存在是不夠的——先前那個缺陷不是「沒有修剪函式」，是**沒有人叫它**。
    這裡從原始碼上確認 `msgs` 的來源真的是 `_dorossi_trim_api_history(...)`，
    而不是又變回 `list(history)`。
    """
    tree = ast.parse((PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_dorossi_via_api"), None)
    assert fn is not None
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "msgs"
                       for t in n.targets)]
    assert assigns, "`_dorossi_via_api` 不再組 `msgs` 了——這支守門要跟著改"
    for node in assigns:
        assert "_dorossi_trim_api_history" in ast.unparse(node.value), (
            f"送出的歷史沒有經過修剪：{ast.unparse(node)[:120]}")


def test_the_other_two_backends_do_not_carry_history_themselves():
    """界限只該掛在 `api` 上。

    `claude_code` 與 `codex` 的脈絡都在後端，我們只帶 session id；如果哪天有人在
    那兩條路上也開始攜帶歷史，那就是另一個無界限的入口，而這支會先變紅。
    """
    text = (PKG_ROOT / "dorossi_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    carriers = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in ("_dorossi_via_api", "_dorossi_trim_api_history"):
            continue
        args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        if "history" in args:
            carriers.append((fn.name, fn.lineno))
    assert not carriers, (
        f"{carriers} 也開始攜帶對話歷史了。界限目前只掛在 `_dorossi_via_api` "
        "上，多一個攜帶者就是多一個沒有上限的入口——請一起接上 "
        "`_dorossi_trim_api_history`，再把這支守門的豁免名單加上去。")
