"""單圖服務——`_webrunner_shared.serve_single_image_request`。

使用者在對話平台上要一張圖，bot 把請求寫成一個檔，背景程式撿起來處理。這一支就是
「處理」的全部。**96 行敘述裡 37 行沒有被跑過**，而它是整條產線上唯一一條**有人在
另一端等著**的路徑：批次可以慢、可以重試、可以等額度回補，單圖不行。

#### 整份契約壓在一條不變式上

磁碟契約寫著：每一個 `request_id` **剛好**對到一個
`single_image_done` 事件。呼叫端無論成敗都會刪掉請求檔，所以

* **少發一則** ＝ 使用者永遠等不到回覆（bot 那邊的 pending 條目只能靠 TTL 逾時清掉，
  而那是「失敗」的措辭，不是「這次不行」）；
* **多發一則** ＝ 同一個 request 被回報兩次。

所以這一檔不是替每一條失敗路徑各寫一句字面斷言，而是**對每一種注入的故障跑同一條
檢查**：事件恰好一則、`request_id` 對得上、`ok` 與是否帶 `path` 一致。這是 §8.109
那條 rc／結果行不變式的同一個做法。

#### 兩條路的角色框處理是**相反**的，而弄反不會當場壞掉

| | idle one-shot（`in_band=False`） | 帶內插播（`in_band=True`） |
|---|---|---|
| 角色框 | 刪到最少、再把殘存的清成空字串 | **絕不刪**，一律用「填」（空值＝清空） |
| 驗證 | 內容導向：殘存框 strip 後都要是空的 | 逐角色 `verify_character_prompt`，連清空也驗 |

弄反的後果都是安靜的：idle 那條若不刪框，上一個批次角色的特徵會滲進這張單圖（實測
症狀是「只有 Character 2 被刪、Character 1 帶舊值」）；帶內那條若刪了框，批次後續的
`_refill_character_fields` 會**靜默失敗**（`fill_character_prompt` 先定位卡片再寫，
卡片不在就回 False），於是那個角色剩下的圖全部以缺框產生。

#### `request_id` 直接拿來當資料夾名

`SINGLE_IMAGE_OUTPUT_ROOT / request_id`，而 `request_id` 來自磁碟上的請求檔。今天安全
靠的是「唯一的寫入者只產十六進位」——那是**寫入端**的性質，中間還隔著一個跨行程的
JSON 檔。判成失敗而不是退回 `unknown/` 也是刻意的：路徑形狀的 id 必然不在 bot 的
correlation map 裡，那張圖不會有人收到，而額度是這條產線的瓶頸。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _webrunner_shared as ws  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """整條路上每一個會碰到瀏覽器／磁碟的東西都換掉。

    唯一的旋鈕是 `env.fail`：一組 `with_retry` 的名字，列在裡面的那一步回 False。
    每一步都由 `with_retry` 包著，所以這一個旋鈕就覆蓋了整張失敗表。
    """
    env = types.SimpleNamespace(
        fail=set(), events=[], beats=[], snaps=[], prints=[],
        areas=["主框", "角色框1"], residual={}, cleared=[], clear_fails=set(),
        clear_raises=set(), clear_noop=set(), removed=0, filled={}, verified=[],
        new_src="新圖", download_ok=True, generate_raises=None,
        dismissed=0, generate_attempts=0,
        out_root=tmp_path / "_oneshot",
    )

    def _with_retry(name, fn, **_kwargs):
        if name in env.fail:
            return False
        return fn()

    def _emit(kind, **payload):
        env.events.append((kind, payload))

    def _beat(request_id, in_band, phase, batch_cfg):
        env.beats.append(phase)

    def _generate(port, previous, **kwargs):
        env.generate_attempts += 1
        on_attempt = kwargs.get("on_attempt")
        if on_attempt is not None:
            on_attempt(1)
        if env.generate_raises is not None:
            raise env.generate_raises
        return env.new_src

    def _download(port, src, target, **_kwargs):
        if not env.download_ok:
            return False
        # 刻意**不**建父目錄：真正的下載也不會。替身順手 mkdir 的話，「呼叫端忘了
        # 先建輸出目錄」這個缺陷就永遠被遮住（量過：那個變異會活下來）。
        Path(target).write_bytes(b"PNG")
        return True

    def _fill_textarea_like(port, area, value):
        if area in env.clear_raises:
            raise RuntimeError("清不動")
        env.cleared.append(area)
        if area in env.clear_fails:
            return False
        if area in env.clear_noop:
            # 「回報成功，但值其實沒有被改掉」——清空最危險的失效形態，因為它
            # 跟成功長得一模一樣。內容導向的驗證是唯一看得出來的東西。
            return True
        env.residual[area] = value
        return True

    monkeypatch.setattr(ws, "with_retry", _with_retry)
    monkeypatch.setattr(ws, "emit_event", _emit)
    monkeypatch.setattr(ws, "_emit_serving_beat", _beat)
    monkeypatch.setattr(ws, "human_pause", lambda *_a, **_k: None)
    monkeypatch.setattr(ws, "load_batch_config", lambda: {
        "generate_max_retries": 11, "generate_retry_delay_sec": 13,
        "download_max_retries": 17})
    monkeypatch.setattr(ws, "fill_main_prompt",
                        lambda _p, text: env.filled.__setitem__("main", text) or True)
    monkeypatch.setattr(ws, "fill_main_undesired",
                        lambda _p, text: env.filled.__setitem__("undesired", text) or True)
    monkeypatch.setattr(
        ws, "fill_character_prompt",
        lambda _p, n, text: env.filled.__setitem__(f"char{n}", text) or True)
    monkeypatch.setattr(
        ws, "verify_character_prompt",
        lambda _p, n, text: env.verified.append((n, text)) or True)
    monkeypatch.setattr(
        ws, "remove_all_character_slots",
        lambda _p: setattr(env, "removed", env.removed + 1))
    monkeypatch.setattr(ws, "find_prompt_areas", lambda _p: list(env.areas))
    monkeypatch.setattr(ws, "fill_textarea_like", _fill_textarea_like)
    monkeypatch.setattr(ws, "_read_textarea_value",
                        lambda _p, area: env.residual.get(area, ""))
    monkeypatch.setattr(ws, "snap",
                        lambda _p, label: env.snaps.append(label))
    monkeypatch.setattr(ws, "get_main_image_src", lambda _p: "舊圖")
    monkeypatch.setattr(ws, "generate_one_image", _generate)
    monkeypatch.setattr(ws, "download_image_with_retry", _download)
    monkeypatch.setattr(ws, "dismiss_blocking_dialog",
                        lambda _p: setattr(env, "dismissed", env.dismissed + 1))
    monkeypatch.setattr(ws, "SINGLE_IMAGE_OUTPUT_ROOT", env.out_root)
    monkeypatch.setattr(ws, "_single_image_relative_path",
                        lambda path: "output/_oneshot/" + Path(path).name)
    return env


def _req(**over) -> dict:
    base = {"request_id": "a1b2c3", "prompt": "一隻貓"}
    base.update(over)
    return base


def _done(env) -> list[dict]:
    return [payload for kind, payload in env.events
            if kind == "single_image_done"]


# ---------------------------------------------------------------------------
# 一、每一個 request_id 剛好一則 done 事件
# ---------------------------------------------------------------------------

_FAILURE_POINTS = [
    "empty_prompt", "unsafe_id",
    "oneshot_fill_main_prompt", "oneshot_fill_undesired",
    "oneshot_fill_char1", "oneshot_fill_char2",
    "oneshot_verify_char1", "oneshot_verify_char2",
    "clear_failed", "generation", "download", "quota", "unexpected",
]


def _arrange(env, how: str) -> dict:
    """把 `env` 弄成指定的故障，回傳要送進去的請求。"""
    if how == "empty_prompt":
        return _req(prompt="   ")
    if how == "unsafe_id":
        return _req(request_id="../../逃出去")
    if how == "clear_failed":
        env.residual["角色框1"] = "上一個批次的角色"
        env.clear_noop.add("角色框1")
        return _req()
    if how == "generation":
        env.new_src = None
        return _req()
    if how == "download":
        env.download_ok = False
        return _req()
    if how == "quota":
        env.generate_raises = ws.GenerationBlockedError("額度用完")
        return _req()
    if how == "unexpected":
        env.generate_raises = ValueError("沒想到的東西")
        return _req()
    env.fail.add(how)
    return _req()


def _needs_in_band(how: str) -> bool:
    return how in ("oneshot_fill_char1", "oneshot_fill_char2",
                   "oneshot_verify_char1", "oneshot_verify_char2")


@pytest.mark.parametrize("how", _FAILURE_POINTS)
def test_every_failure_still_emits_exactly_one_done_event(serve, how):
    """**本檔最重要的一條。**

    呼叫端無論成敗都會刪掉請求檔，所以少發一則 ＝ 使用者永遠等不到回覆（只能靠
    bot 那邊的 TTL 逾時，而那是「失敗」的措辭）；多發一則 ＝ 同一筆被回報兩次。
    """
    req = _arrange(serve, how)
    ws.serve_single_image_request(object(), req, in_band=_needs_in_band(how))
    done = _done(serve)
    assert len(done) == 1, f"`{how}` 發了 {len(done)} 則 done 事件"
    assert done[0]["ok"] is False
    assert done[0]["request_id"] == req["request_id"]
    assert done[0].get("error"), "失敗事件沒帶原因"
    assert "path" not in done[0], "失敗事件不該帶路徑"


def test_a_successful_serve_emits_one_done_with_the_path(serve):
    ws.serve_single_image_request(object(), _req())
    done = _done(serve)
    assert len(done) == 1
    assert done[0]["ok"] is True
    assert done[0]["path"].endswith(".png")
    assert "error" not in done[0]


def test_the_image_lands_under_the_request_id(serve):
    ws.serve_single_image_request(object(), _req(request_id="deadbeef"))
    written = list((serve.out_root / "deadbeef").glob("*.png"))
    assert len(written) == 1, f"輸出不是剛好一張：{written}"


# ---------------------------------------------------------------------------
# 二、`request_id` 直接當資料夾名
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../逃出去", "C:/Windows/Temp/x", "a/b", "..", "NUL", "a.", "a\tb",
])
def test_a_path_shaped_request_id_is_refused_before_anything_is_created(serve, bad):
    """`SINGLE_IMAGE_OUTPUT_ROOT / request_id` 直接把它當一層資料夾名。

    實測過 `C:\\Windows\\Temp\\x` 會**整個取代** base（`output/_oneshot` 消失）、
    `../../..` 走得出 repo，而緊接著就是 `mkdir(parents=True)` 加上把下載的圖寫
    進去。今天安全靠的是「唯一的寫入者只產十六進位」——那是**寫入端**的性質，
    中間還隔著一個跨行程的 JSON 檔。
    """
    ws.serve_single_image_request(object(), _req(request_id=bad))
    assert _done(serve)[0]["error"] == "invalid request id"
    assert serve.generate_attempts == 0, "已經去產圖了"
    assert not serve.out_root.exists(), "建立了輸出目錄"


def test_a_broken_request_file_still_reports_the_empty_prompt(serve):
    """請求檔壞掉時 `req` 是 `{}`，`request_id` 與 `prompt` 同時為空。

    id 檢查排在 prompt 檢查**之後**是刻意的：這種情形要繼續報 `empty prompt`，
    既有的診斷語意才不會變。
    """
    ws.serve_single_image_request(object(), {})
    assert _done(serve)[0]["error"] == "empty prompt"


def test_a_refused_request_never_claims_to_be_serving(serve):
    """兩道驗證沒過的請求不發「服務中」——沒有在服務就不要說在服務，否則 bot 會
    把等待時間重新計時，使用者反而等更久。"""
    ws.serve_single_image_request(object(), _req(prompt=""))
    assert serve.beats == []
    ws.serve_single_image_request(object(), _req(request_id="../x"))
    assert serve.beats == []


def test_a_served_request_announces_itself_before_touching_the_browser(serve):
    """第一則「服務中」要在碰瀏覽器之前發：bot 要知道的是「有人開始處理這一筆」。"""
    ws.serve_single_image_request(object(), _req())
    assert serve.beats[0] == "start"
    assert "generate" in serve.beats and "download" in serve.beats


def test_the_serving_beat_is_sent_on_every_generate_attempt(serve):
    """一次服務可以跑滿 `generate_max_retries` 次嘗試（預設約 15 分鐘），只靠開頭
    那一則撐不住 bot 的存活判定。"""
    ws.serve_single_image_request(object(), _req())
    assert serve.beats.count("generate") == 1  # 假的 generate 只跑一次 attempt


# ---------------------------------------------------------------------------
# 三、兩條路的角色框處理是相反的
# ---------------------------------------------------------------------------

def test_an_idle_one_shot_deletes_the_character_slots(serve):
    """不刪的話，上一個批次角色的特徵會滲進這張單圖（實測症狀：只有 Character 2
    被刪、Character 1 帶舊值）。"""
    ws.serve_single_image_request(object(), _req(char1="甲", char2="乙"))
    assert serve.removed == 1
    assert "char1" not in serve.filled, "idle 路徑不該填任何角色內容"
    assert "char2" not in serve.filled


def test_an_idle_one_shot_clears_whatever_slot_survives(serve):
    """NovelAI 強制至少保留一個角色框（最後一框沒有刪除鈕），所以「刪到 0」做不到。
    正確收尾是刪到最小**再把殘存框清成空字串**。"""
    serve.areas = ["主框", "角色框1", "角色框2"]
    ws.serve_single_image_request(object(), _req())
    assert serve.cleared == ["角色框1", "角色框2"], serve.cleared


def test_a_clear_that_lies_about_succeeding_is_caught_by_the_content_check(serve):
    """**清空最危險的失效是「回報成功但值沒改」**——它跟成功長得一模一樣。

    所以判準是內容導向的：殘存的每個角色框 strip 之後都要是空的。用
    `count_characters == 0` 當成功判準是錯的（最後一框本來就刪不掉），這一條
    在函式的註解裡已經寫過一次，這裡釘住它。
    """
    serve.residual["角色框1"] = "殘留的角色"
    serve.clear_noop.add("角色框1")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["error"] == "character area clear failed"
    assert serve.generate_attempts == 0
    assert serve.snaps, "失敗時沒有留下畫面快照"


def test_a_slot_that_raises_while_clearing_also_fails_the_request(serve):
    """清空是 best-effort、不 raise，但「有一格清不動」仍然必須讓整筆失敗。"""
    serve.clear_raises.add("角色框1")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["error"] == "character area clear failed"


def test_a_slot_that_reports_failure_without_raising_also_fails_the_request(serve):
    """`fill_textarea_like` 回 False（沒有例外）也算清失敗。

    這一條與上一條分得開：一個走 `except`，一個走 `and clear_ok`，而兩者都只在
    「殘存內容已經是空的」時才是唯一的判準——沒有它，一次靜默失敗的清空會被
    內容導向的驗證放行。
    """
    serve.clear_fails.add("角色框1")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["error"] == "character area clear failed"


def test_a_whitespace_only_slot_counts_as_cleared(serve):
    """判準是 strip 之後為空。

    少了 `.strip()`，一個只剩空白的角色框會被當成「還有內容」——於是**每一次**
    idle 單圖都失敗，而畫面上那一格看起來是空的。
    """
    serve.residual["角色框1"] = "   "
    serve.clear_noop.add("角色框1")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["ok"] is True


def test_the_output_directory_is_created_before_the_download(serve):
    """下載端不會幫忙建目錄。少了這一步，成功產出的那張圖寫不進去。"""
    ws.serve_single_image_request(object(), _req(request_id="feed01"))
    assert (serve.out_root / "feed01").is_dir()


def test_an_in_band_serve_never_deletes_a_slot(serve):
    """**刪框會讓批次的 refill 靜默失敗。**

    `_refill_character_fields` 走 `fill_character_prompt`，它先定位角色卡片再寫，
    卡片不在就回 False（只印一行）——於是那個角色剩下的圖全部以缺框產生。
    """
    ws.serve_single_image_request(object(), _req(char1="甲", char2="乙"),
                                  in_band=True)
    assert serve.removed == 0
    assert serve.cleared == []


def test_an_in_band_serve_fills_both_character_slots(serve):
    """帶內路徑一律「填」，空值就是清空——框留著，refill 仍然可用。"""
    ws.serve_single_image_request(object(), _req(char1="甲", char2=""),
                                  in_band=True)
    assert serve.filled["char1"] == "甲"
    assert serve.filled["char2"] == ""


def test_an_in_band_serve_verifies_both_slots_including_the_empty_one(serve):
    """連「清空」也要驗（expected=""）。

    少了這一步，上一個批次角色會因為**一次靜默失敗的清空**被產進這張單圖——而
    清空失敗看起來跟成功一模一樣。
    """
    ws.serve_single_image_request(object(), _req(char1="甲", char2=""),
                                  in_band=True)
    assert serve.verified == [(1, "甲"), (2, "")]


def test_an_idle_one_shot_verifies_no_individual_character(serve):
    """idle 路徑的框已經全刪＋清空，改用內容導向的驗證；再逐角色驗會對著不存在的
    卡片問，必然失敗。"""
    ws.serve_single_image_request(object(), _req(char1="甲"))
    assert serve.verified == []


# ---------------------------------------------------------------------------
# 四、主 prompt 與 undesired
# ---------------------------------------------------------------------------

def test_the_main_prompt_and_undesired_are_written(serve):
    ws.serve_single_image_request(object(), _req(prompt="一隻貓", undesired="模糊"))
    assert serve.filled["main"] == "一隻貓"
    assert serve.filled["undesired"] == "模糊"


def test_a_missing_undesired_clears_the_field(serve):
    """沒給就要清空，不能留著上一個批次的負面詞——那會安靜地改掉這張圖的內容。"""
    ws.serve_single_image_request(object(), _req())
    assert serve.filled["undesired"] == ""


def test_the_generation_budget_comes_from_the_batch_config(serve, monkeypatch):
    seen = {}

    def _generate(port, previous, **kwargs):
        seen.update(kwargs)
        return "新圖"

    monkeypatch.setattr(ws, "generate_one_image", _generate)
    ws.serve_single_image_request(object(), _req())
    assert seen["max_retries"] == 11 and seen["retry_delay"] == 13


def test_the_download_budget_comes_from_the_batch_config(serve, monkeypatch):
    seen = {}

    def _download(port, src, target, **kwargs):
        seen.update(kwargs)
        Path(target).write_bytes(b"PNG")
        return True

    monkeypatch.setattr(ws, "download_image_with_retry", _download)
    ws.serve_single_image_request(object(), _req())
    assert seen["max_retries"] == 17


# ---------------------------------------------------------------------------
# 五、三種例外，三種不同的處置
# ---------------------------------------------------------------------------

def test_a_quota_block_is_reported_and_the_dialog_is_dismissed(serve):
    """批次可以關掉對話框慢慢等額度回補，單圖不行——使用者正在等一則回覆。

    但**一定要把對話框關掉**：留著會擋住後面接著跑的批次。
    """
    serve.generate_raises = ws.GenerationBlockedError("額度用完")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["error"] == "quota unavailable"
    assert serve.dismissed == 1, "對話框留在畫面上，後面的批次會被擋住"


def test_a_lost_browser_is_reported_and_then_re_raised(serve):
    """瀏覽器整個沒了：先把這一筆收乾淨（契約要求剛好一則事件），再往上拋讓外層
    收工重生。吞掉它會讓後面每一筆請求／每一張批次圖都對著死瀏覽器空轉。"""
    serve.generate_raises = ws.BrowserGoneError("瀏覽器不見了")
    with pytest.raises(ws.BrowserGoneError):
        ws.serve_single_image_request(object(), _req())
    done = _done(serve)
    assert len(done) == 1 and done[0]["error"] == "browser session lost"


def test_an_unexpected_error_is_swallowed_after_reporting(serve):
    """其他例外**不能**往上拋：那會把一次單圖服務變成整輪批次的終止。"""
    serve.generate_raises = ValueError("沒想到的東西")
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["ok"] is False


def test_an_unexpected_error_message_is_truncated(serve):
    """對外的 `error` 欄位是要送進事件檔再被 bot 讀的，不能放一整段 traceback。"""
    serve.generate_raises = ValueError("長" * 500)
    ws.serve_single_image_request(object(), _req())
    assert len(_done(serve)[0]["error"]) <= 200


# ---------------------------------------------------------------------------
# 六、絕不碰批次的續產狀態
# ---------------------------------------------------------------------------

def test_the_resume_checkpoint_is_never_touched(serve, monkeypatch):
    """`webrunner_progress.json` 是批次角色續產用的，單圖不該污染它。

    寫進去的後果是：批次重啟之後從一個錯的角色接下去，而磁碟上看起來完全正常。
    """
    def _boom(*_a, **_k):
        raise AssertionError("單圖服務碰了批次的續產檢查點")

    for name in ("save_progress", "write_progress", "_atomic_write"):
        if hasattr(ws, name):
            monkeypatch.setattr(ws, name, _boom)
    ws.serve_single_image_request(object(), _req())
    assert _done(serve)[0]["ok"] is True


# ---------------------------------------------------------------------------
# 七、`_emit_serving_beat`——遙測絕不可以讓一張圖失敗
# ---------------------------------------------------------------------------

@pytest.fixture
def beat(monkeypatch):
    env = types.SimpleNamespace(events=[], emit_raises=None, within_raises=None)

    def _emit(kind, **payload):
        if env.emit_raises is not None:
            raise env.emit_raises
        env.events.append((kind, payload))

    def _within(cfg):
        if env.within_raises is not None:
            raise env.within_raises
        return 42.0

    monkeypatch.setattr(ws, "emit_event", _emit)
    monkeypatch.setattr(ws, "_serving_beat_within_sec", _within)
    return env


def test_the_beat_carries_what_the_bot_needs_to_tell_slow_from_dead(beat):
    """bot 靠這則事件分辨「正在服務、只是慢」與「服務者已經死了」。

    少了 `beat_within_sec`，bot 只量得到「距離送出多久」——而那個量對帶內服務是
    錯的（服務者要等批次的當前步驟結束才輪得到它）。
    """
    ws._emit_serving_beat("abc", True, "generate", {})
    kind, payload = beat.events[0]
    assert kind == "single_image_serving"
    assert payload["request_id"] == "abc"
    assert payload["in_band"] is True
    assert payload["phase"] == "generate"
    assert payload["beat_within_sec"] == 42.0


def test_an_unserializable_in_band_flag_is_normalised(beat):
    """`in_band` 要送出**布林**——事件檔是 JSON，而呼叫端傳進來的可能是任何真值。"""
    ws._emit_serving_beat("abc", 1, "start", {})
    assert beat.events[0][1]["in_band"] is True


def test_a_beat_that_cannot_be_written_never_breaks_the_serve(beat, capsys):
    """**這一支在服務的 `try` 裡面被呼叫**，還會經由 `on_attempt` 在
    `generate_one_image` 裡面被呼叫。

    從這裡逸出的任何東西都會被 `serve_single_image_request` 的 broad except 接住，
    於是一次好好的服務變成 `ok=false`。訊號寫不出去的代價是「bot 可能提早放棄」，
    拿一張圖去換不划算。
    """
    beat.emit_raises = RuntimeError("事件檔鎖住了")
    ws._emit_serving_beat("abc", False, "start", {})
    assert "serve continues" in capsys.readouterr().err


def test_the_promise_arithmetic_is_inside_the_same_guard(beat, capsys):
    """承諾的計算也在 `try` 裡面，理由相同——設定檔裡一個壞掉的值不該害一張圖失敗。

    這一條與上一條分得開：上一條的例外來自 `emit_event`，這一條來自算 `within`，
    而那一步發生在 `emit_event` **之前**。
    """
    beat.within_raises = ZeroDivisionError("設定檔裡是 0")
    ws._emit_serving_beat("abc", False, "download", {})
    assert beat.events == []
    assert "serve continues" in capsys.readouterr().err
