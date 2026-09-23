"""兩個設定載入器都說自己「never raises」、也都說壞值會退回預設——實測兩句都不成立。

`batch_config.json` 與 `bot_config.json` 都是**明擺著給人手動編輯**的檔案（前者的註解
自己就寫「就直接編輯 `batch_config.json`」），而 `/config set` 又是一條使用者面的寫入
路徑。所以載入器的型別檢查就是這兩個檔案的全部防線。2026-08-30 量出兩個洞，兩個都是
JSON 就進得來、而且靜悄悄的：

**一、`inf` / `nan`。** `Infinity` / `NaN` 是 Python 對 JSON 的擴充，`json.loads` 預設
就吃；而且根本不必有人手打 `Infinity`——`1e400` 這種看起來完全正常的字面值 parse 出來
就是 `inf`。所有檢查都長成 `isinstance(v, (int, float)) and v > 0`，而 `inf > 0` 為真，
於是一路放行：

- `inter_image_delay_sec: [Infinity, Infinity]` → `random.uniform(inf, inf)` 回 **nan**
  （`inf + (inf-inf)*x`）→ `time.sleep(nan)` 丟 `ValueError`，角色迴圈在**第一次**圖間
  等待就炸掉，`main()` 非零離開、監督者重啟、再炸一次，直到 rapid-fail 放棄。
- `dorossi_cc_hard_limit_*_sec: Infinity` → watchdog 上限變成無限大，等於**關掉**
  watchdog。`_coerce_clamped_num` 的 docstring 說得很清楚，那正是它要防的事。
- `nan` 更陰險：bot 的 parser 收下、回報「設定成功」，載入器卻把它退回預設，於是使用者
  看到成功訊息但值從來沒有生效。這正是那些 `_parse_cfg_*` 存在的理由（原始碼註解寫著
  「a value the bot accepts but the loader rejects would silently revert」），
  而 `_parse_cfg_pair` 是唯一漏掉 `math.isfinite` 的那一支。
- 寫回磁碟時 `json.dumps` 預設會產生裸的 `Infinity` / `NaN`——**那不是合法 JSON**。
  Python 讀得回來，`jq`／瀏覽器／編輯器的 JSON 檢查一律讀不了。

**二、大到轉不成 float 的整數。** JSON 的整數沒有位數上限，`json.loads` 會給一個任意
精度的 Python int，而 `float(10**400)` 與 `math.isfinite(10**400)` 都丟 `OverflowError`。
於是 `load_batch_config` / `load_bot_config` **會 raise**，兩者的 docstring 都寫著不會。
`load_bot_config` 是在 bot import 時跑的，所以那等於 bot 起不來——而症狀只有一個
`OverflowError: int too large to convert to float`，看不出跟設定檔有關。

修法是一個共用的述詞 `_is_finite_number`（兩個模組各一份，因為它們不互相 import），
而且它自己必須對超大 int 安全：**只用比較**（int 與 float 比大小是精確的、不會溢位），
不要呼叫 `float()` 或 `math.isfinite()`。

⚠️ **不要改用 `json.loads(..., parse_constant=…)`——那是網路上的標準建議，而它在這裡
擋不到真正會發生的輸入。** 2026-09-10 實測（CPython 3.14.4）：

| 輸入 | `parse_constant` 有沒有被呼叫 | 結果 |
|---|---|---|
| `{"v": Infinity}` | 有 | 擋得掉 |
| `{"v": NaN}` / `{"v": -Infinity}` | 有 | 擋得掉 |
| `{"v": 1e400}` | **沒有** | `inf` |
| `{"v": 1e999}` / `{"v": -1e400}` | **沒有** | `±inf` |
| `{"v": 10**400}`（400 位數的整數） | **沒有** | 任意精度 int |

`parse_constant` 只在 **tokenizer 讀到那三個字面 token** 時才被呼叫；`1e400` 是一個
完全正常的數字 token，溢位發生在後面的 `float()` 轉換，parser 不覺得有事。而**沒有
人會手打 `Infinity`**——會出現在手編設定檔裡的是多打幾個零的指數。所以 parser 層的
防線剛好漏掉唯一實際會發生的那一種，而且會給人「已經處理過了」的錯覺。
**驗證要在值的層級做**，也就是 `_is_finite_number`。

（寫出去的那一側是另一回事，那裡 `allow_nan=False` 有效——見
`_batch_config._atomic_write_config`。）

這支測試的清單是**從 AST 推出來的**，不是手寫的：任何新的 `_coerce_*` 只要對數字做
`isinstance` 檢查就會自動被要求進表，漏掉會紅。手寫清單會爛掉，這一點在同一輪的
`verify_external_apis` 端點清單上已經吃過一次虧。
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import math
import os
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _batch_config as bc  # noqa: E402
import _warn_dedup                      # noqa: E402
import _bot_config as bo  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"

BIG_INT = 10 ** 400          # json.loads 給得出來，float() 轉不過去
INF = float("inf")
NAN = float("nan")

# 非有限的數字，逐一餵給每一個數值 coercer。
NON_FINITE = [
    pytest.param(INF, id="inf"),
    pytest.param(-INF, id="-inf"),
    pytest.param(NAN, id="nan"),
    pytest.param(BIG_INT, id="huge-int"),
    pytest.param(-BIG_INT, id="huge-negative-int"),
]

# 明確豁免的 `_coerce_*`：它處理的是「識別碼」不是「量」，而且從不呼叫 `float()`，
# 所以超大整數只會變成一個對不上任何人的 id，不會溢位也不會影響任何計算。
# 豁免是**列舉制**：要豁免第二支就得在這裡寫下理由。
_EXEMPT = {
    "_coerce_int_list": "使用者 id 清單——是識別碼不是量，而且從不 float()",
    # 其他平台的使用者／對話 id。同一個理由，只是值是字串：它做
    # `isinstance(one, (str, int))` 所以被掃到，但整支只呼叫 `str()`，一個超大整數
    # 只會變成一串對不上任何人的數字，不會溢位、也不參與任何計算。
    "_coerce_str_list": "平台識別字清單——是識別碼不是量，只呼叫 str()",
}


def _numeric_coercers(path: Path) -> set[str]:
    """AST 掃出「會對數字做型別檢查」的 `_coerce_*`。

    判準：函式體裡出現 `isinstance(x, int)` / `isinstance(x, float)` /
    `isinstance(x, (int, float))`，或呼叫了 `_is_finite_number`。這條規則機械可判定，
    而且新增一個數值 coercer 一定會命中——手寫名單才是會爛掉的那種。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("_coerce_"):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fname = getattr(sub.func, "id", None)
            if fname == "_is_finite_number":
                found.add(node.name)
                break
            if fname == "isinstance" and len(sub.args) == 2:
                names = _type_names(sub.args[1])
                if names & {"int", "float"}:
                    found.add(node.name)
                    break
    return found


def _type_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Tuple):
        return {e.id for e in node.elts if isinstance(e, ast.Name)}
    return set()


def _wrap_pair(value):
    return [value, value]


# name → (module, callable, kwargs, 把壞值包成該 coercer 吃的形狀)
_TABLE: dict[str, tuple] = {
    "_batch._coerce_pair": (bc._coerce_pair, {}, _wrap_pair, (20.0, 30.0)),
    "_batch._coerce_int": (bc._coerce_int, {}, None, 240),
    "_batch._coerce_positive_num": (bc._coerce_positive_num, {}, None, 6.0),
    "_batch._coerce_non_negative_num": (
        bc._coerce_non_negative_num, {}, None, 0.0),
    "_batch._coerce_ratio": (bc._coerce_ratio, {}, None, 0.9),
    "_bot._coerce_int": (bo._coerce_int, {}, None, 7),
    "_bot._coerce_positive_num": (bo._coerce_positive_num, {}, None, 1.5),
    "_bot._coerce_nonneg_num": (bo._coerce_nonneg_num, {}, None, 2.5),
    "_bot._coerce_clamped_num": (
        bo._coerce_clamped_num, {"min_value": 60.0}, None, 900.0),
}


# --- 清單本身要對得起來（雙向）-----------------------------------------------

def test_every_numeric_coercer_is_covered():
    for module_name, path, prefix in (
            ("_batch_config", PKG_ROOT / "_batch_config.py", "_batch."),
            ("_bot_config", PKG_ROOT / "_bot_config.py", "_bot.")):
        found = _numeric_coercers(path) - set(_EXEMPT)
        listed = {n[len(prefix):] for n in _TABLE if n.startswith(prefix)}
        missing = found - listed
        assert not missing, (
            f"{module_name} 新增了數值 coercer {sorted(missing)} 但沒有進 `_TABLE`。"
            "設定檔是使用者手改的，型別檢查就是全部的防線——每一支都要證明它擋得掉 "
            "inf/nan 與轉不成 float 的超大整數。真的不適用就寫進 `_EXEMPT` 並附理由。")


def test_the_table_does_not_list_coercers_that_are_gone():
    for path, prefix in ((PKG_ROOT / "_batch_config.py", "_batch."),
                         (PKG_ROOT / "_bot_config.py", "_bot.")):
        found = _numeric_coercers(path)
        listed = {n[len(prefix):] for n in _TABLE if n.startswith(prefix)}
        stale = listed - found
        assert not stale, (
            f"`_TABLE` 還列著已經不存在（或不再做數值檢查）的 {sorted(stale)}")


def test_the_exemptions_still_exist():
    everything = (_numeric_coercers(PKG_ROOT / "_batch_config.py")
                  | _numeric_coercers(PKG_ROOT / "_bot_config.py"))
    stale = set(_EXEMPT) - everything
    assert not stale, f"豁免名單裡的 {sorted(stale)} 已經不存在了，刪掉它"


# --- 每一支 coercer 都要擋掉非有限數字 ---------------------------------------

@pytest.mark.parametrize("name", sorted(_TABLE))
@pytest.mark.parametrize("bad", NON_FINITE)
def test_a_non_finite_value_never_survives_a_coercer(name, bad):
    fn, kwargs, wrap, default = _TABLE[name]
    value = wrap(bad) if wrap else bad
    got = fn(value, default, **kwargs)
    assert got == default or got is default, (
        f"{name} 放行了 {bad!r} → {got!r}。這種值會一路流到 `time.sleep()` "
        "或 watchdog 上限，而且沒有任何錯誤訊息。")


@pytest.mark.parametrize("name", sorted(_TABLE))
def test_a_normal_value_still_gets_through(name):
    """反面：守門不能守到把正常值也擋掉。"""
    fn, kwargs, wrap, default = _TABLE[name]
    good = 0.5 if "ratio" in name else (3 if "_int" in name else 3.0)
    value = wrap(good) if wrap else good
    got = fn(value, default, **kwargs)
    assert got != default, f"{name} 把正常值 {good!r} 也擋掉了"


@pytest.mark.parametrize("name", ["_batch._coerce_int", "_bot._coerce_int"])
def test_an_int_coercer_still_rejects_a_float(name):
    """守門加上去之後，`isinstance(value, int)` 那一半不能跟著不見——
    `images_per_character: 3.5` 會一路變成 `range(3.5)` 的 TypeError。"""
    fn, kwargs, _wrap, default = _TABLE[name]
    assert fn(3.5, default, **kwargs) == default


# --- `_is_finite_number` 自己 -------------------------------------------------

@pytest.mark.parametrize("module", [bc, bo], ids=["_batch", "_bot"])
def test_the_predicate_never_raises_on_a_huge_int(module):
    """述詞自己不能用 `float()` / `math.isfinite()`——那兩個對超大 int 會溢位，
    而它正是為了擋超大 int 才存在的。"""
    assert module._is_finite_number(BIG_INT) is False
    assert module._is_finite_number(-BIG_INT) is False


@pytest.mark.parametrize("module", [bc, bo], ids=["_batch", "_bot"])
def test_a_bool_is_not_a_number(module):
    """`bool` 是 `int` 的子類：`True` 不擋掉就會變成 1。"""
    assert module._is_finite_number(True) is False
    assert module._is_finite_number(False) is False


@pytest.mark.parametrize("module", [bc, bo], ids=["_batch", "_bot"])
def test_ordinary_numbers_are_finite(module):
    for value in (0, 1, -1, 3.5, sys.float_info.max, 10 ** 18):
        assert module._is_finite_number(value) is True, value


@pytest.mark.parametrize("module", [bc, bo], ids=["_batch", "_bot"])
def test_a_string_is_not_a_number(module):
    assert module._is_finite_number("3") is False
    assert module._is_finite_number(None) is False


def test_the_two_copies_agree():
    """兩個模組不互相 import，所以述詞有兩份。行為分歧就是一個等著發生的 bug。"""
    for value in (INF, -INF, NAN, BIG_INT, -BIG_INT, True, False, 0, 1, 3.5,
                  "3", None, [], sys.float_info.max):
        assert bc._is_finite_number(value) is bo._is_finite_number(value), value


# --- 端到端：載入器真的不 raise、真的退回預設 --------------------------------

def _write_batch(tmp_path, monkeypatch, payload: str):
    path = tmp_path / "batch_config.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", path)
    monkeypatch.setattr(bc, "_BATCH_CONFIG_TMP",
                        tmp_path / "batch_config.json.tmp")
    return path


def test_a_huge_integer_does_not_take_the_batch_loader_down(
        tmp_path, monkeypatch):
    _write_batch(tmp_path, monkeypatch,
                 '{"rest_hours": ' + "9" * 400 + '}')
    cfg = bc.load_batch_config()          # 不得 raise
    assert cfg["rest_hours"] == bc._DEFAULT_BATCH_CONFIG["rest_hours"]


def test_a_huge_integer_does_not_take_the_bot_loader_down(
        tmp_path, monkeypatch):
    path = tmp_path / "bot_config.json"
    path.write_text('{"event_poll_seconds": ' + "9" * 400 + '}',
                    encoding="utf-8")
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", path)
    cfg = bo.load_bot_config()            # 不得 raise —— 這個是在 bot import 時跑的
    assert cfg["event_poll_seconds"] > 0
    assert math.isfinite(cfg["event_poll_seconds"])


def test_infinity_in_the_file_falls_back_to_the_default(tmp_path, monkeypatch):
    _write_batch(tmp_path, monkeypatch,
                 '{"inter_image_delay_sec": [Infinity, Infinity], '
                 '"rest_hours": Infinity}')
    cfg = bc.load_batch_config()
    lo, hi = cfg["inter_image_delay_sec"]
    assert math.isfinite(lo) and math.isfinite(hi)
    assert math.isfinite(cfg["rest_hours"])


def test_every_loaded_number_is_finite(tmp_path, monkeypatch):
    """把每一個數值鍵都灌成 Infinity，載入器出來的東西不得有任何一個非有限值。"""
    hostile = {k: INF for k, v in bc._DEFAULT_BATCH_CONFIG.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    hostile["inter_image_delay_sec"] = [INF, INF]
    hostile["generate_retry_delay_sec"] = [INF, INF]
    _write_batch(tmp_path, monkeypatch,
                 json.dumps(hostile, allow_nan=True))
    for key, value in bc.load_batch_config().items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                assert math.isfinite(v), f"{key} 載入後是 {v!r}"


# --- 寫入端 -------------------------------------------------------------------

def test_the_writer_refuses_to_produce_invalid_json(tmp_path, monkeypatch):
    """`Infinity` / `NaN` 不是合法 JSON。寧可 raise，也不要把壞資料寫上磁碟——
    這個檔案是給人手動編輯的，壞在磁碟上比當場失敗難救得多。"""
    path = _write_batch(tmp_path, monkeypatch, "{}")
    with pytest.raises(ValueError):
        bc._atomic_write_config({"rest_hours": INF})
    assert path.read_text(encoding="utf-8") == "{}", "壞資料還是寫進去了"


def test_a_refused_write_leaves_no_temp_behind(tmp_path, monkeypatch):
    _write_batch(tmp_path, monkeypatch, "{}")
    with pytest.raises(ValueError):
        bc._atomic_write_config({"rest_hours": NAN})
    assert not (tmp_path / "batch_config.json.tmp").exists()


def test_a_normal_write_still_works(tmp_path, monkeypatch):
    path = _write_batch(tmp_path, monkeypatch, "{}")
    bc._atomic_write_config({"rest_hours": 8})
    assert json.loads(path.read_text(encoding="utf-8"))["rest_hours"] == 8


# --- bot 的 parser 與載入器必須一致 ------------------------------------------

@pytest.mark.parametrize("text", [
    "inf,inf", "nan,nan", "1e400,1e400", "-inf,inf", "0,inf", "20,nan",
])
def test_the_bot_rejects_a_non_finite_pair(text):
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    assert value is None, f"bot 收下了 {text!r} → {value!r}"
    assert err


@pytest.mark.parametrize("text", ["20,30", "20 30", "0,1", "1.5,2.5"])
def test_the_bot_still_accepts_an_ordinary_pair(text):
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    assert err == ""
    assert value is not None


def test_nothing_the_bot_accepts_silently_reverts(tmp_path, monkeypatch):
    """核心的反漂移測試：bot 收下的值，載入器**必須**原封不動地認得。

    兩邊的規則是手抄的兩份（`_parse_cfg_*` vs `_coerce_*`），原始碼註解也寫著要對齊。
    對不齊的下場不是報錯而是**假的成功**：指令回「set ok」，下一次角色迭代載入器把它
    退回預設，使用者要等到看見行為不對才會發現。
    """
    import discord_bot as b

    samples = {
        "images_per_character": ["1", "60", "240"],
        "inter_image_delay_sec": ["20,30", "0,1", "1.5,2.5"],
        "schedule_limit_hours": ["16", "0.5"],
        "rest_hours": ["6", "0.25"],
        "generate_max_retries": ["1", "4"],
        "generate_retry_delay_sec": ["25,30"],
        "download_max_retries": ["1", "3"],
        "consecutive_fail_abort": ["6", "10"],
        "restart_chrome_every_n_characters": ["0", "1", "5"],
        "min_save_ratio": ["0.9", "1"],
        "debug_screenshots": ["true", "false"],
    }
    assert set(samples) == set(b._BATCH_SETTERS), (
        "`_BATCH_SETTERS` 變了但這裡的樣本沒跟上——新的可設定鍵一樣要證明不會靜默回退")

    _write_batch(tmp_path, monkeypatch, "{}")
    for key, texts in samples.items():
        for text in texts:
            value, err = b._BATCH_SETTERS[key](text)
            assert err == "", f"{key}={text!r} 被 bot 擋掉了：{err}"
            applied = bc.save_batch_config({key: value})[key]
            if isinstance(value, list):
                assert [float(v) for v in applied] == [float(v) for v in value], (
                    f"{key}={text!r}：bot 收下 {value!r}，載入器卻給 {applied!r}")
            else:
                assert applied == value, (
                    f"{key}={text!r}：bot 收下 {value!r}，載入器卻給 {applied!r}"
                    "——這是一個回報成功但沒有生效的設定")


# --- 2026-09-21：差分探針找到的兩個分歧 ----------------------------------------
#
# 上面那支用的是手挑的正常樣本，所以兩個都看不到：
#
# 一、**超大整數。** `int()` 收得下幾千位數的字串，載入器的 `_coerce_int` 只認轉得成
#     有限 float 的整數。309 位數的 `9…9` 原本會回報「set ok」、真的寫進檔案，之後
#     每一次重新載入都靜靜退回預設——五個整數鍵全部如此。
# 二、**區間值的負號被吃掉。** `_parse_cfg_pair` 為了支援 `3-5` 把 `-` 一律當分隔符，
#     於是 `-3,5` 存成 3–5、`0,-3` 存成 0–3，`lo < 0` 那道檢查永遠等不到負數；
#     反過來 `1e-3,5` 被拆成三段而擋掉。載入器看到的是一個合法的 `[3, 5]`，所以這一條
#     不是「靜靜退回」，是**存成使用者沒打的東西**——載入器那一側的對帳看不出來。

_HUGE_INT_TEXT = "9" * 309          # 大於 float 上限，`int()` 照收
_LARGEST_HELD_TEXT = "9" * 308      # 小於 float 上限：載入器認得，不得被誤擋
_INT_SETTER_KEYS = [
    "images_per_character",
    "generate_max_retries",
    "download_max_retries",
    "consecutive_fail_abort",
    "restart_chrome_every_n_characters",
]


def test_the_int_setter_list_covers_every_int_key():
    """下面兩支逐鍵測試的清單，必須等於「bot 設得了、而載入器走 `_coerce_int`」的鍵。

    不然新加一個整數鍵時它不會被測到，而那正是這個分歧會再出現的地方。
    """
    import discord_bot as b

    derived = {k for k in b._BATCH_SETTERS if bc._COERCERS[k][0] is bc._coerce_int}
    assert len(derived) >= 5, f"只推出 {sorted(derived)}——`_COERCERS` 的形狀變了？"
    assert set(_INT_SETTER_KEYS) == derived, (set(_INT_SETTER_KEYS) ^ derived)


@pytest.mark.parametrize("key", _INT_SETTER_KEYS)
def test_the_bot_rejects_an_integer_the_loader_cannot_hold(key):
    import discord_bot as b

    # 前提：載入器真的會拒絕它。少了這一行，哪天載入器放寬了，這支還是綠的、
    # 卻已經在釘一個不再需要的限制。
    assert int(_HUGE_INT_TEXT) > sys.float_info.max
    assert bc._coerce_int(int(_HUGE_INT_TEXT), bc._REJECTED) is bc._REJECTED

    value, err = b._BATCH_SETTERS[key](_HUGE_INT_TEXT)
    assert value is None, f"{key}：bot 收下了一個載入器會丟掉的整數"
    assert err and _HUGE_INT_TEXT not in err, err


@pytest.mark.parametrize("key", _INT_SETTER_KEYS)
def test_the_largest_integer_the_loader_can_hold_still_gets_through(
        key, tmp_path, monkeypatch):
    """反方向（近似反例）：剛好在上限內的整數照收，而且寫進去讀得回來、值不變。"""
    import discord_bot as b

    value, err = b._BATCH_SETTERS[key](_LARGEST_HELD_TEXT)
    assert err == "" and value == int(_LARGEST_HELD_TEXT), (value, err)
    _write_batch(tmp_path, monkeypatch, "{}")
    applied = bc.save_batch_config({key: value})[key]
    assert applied == value and type(applied) is int, applied


@pytest.mark.parametrize("text", [
    "-3,5", "0,-3", "3--5", "-0.5,1",
    "-3 5", "3 -5", "-3-5", "3–-5",
])
def test_the_bot_rejects_a_pair_with_a_negative_number(text):
    """負號屬於它後面那個數字，所以這些都是「有一個負數」，一律擋下。

    比對的是**錯誤訊息本身**：`need 0 ≤ lo ≤ hi` 代表負號真的被讀成負號、是範圍檢查
    擋下的；若變成 `need two numbers`，代表切分壞了、只是碰巧擋掉。
    """
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    assert value is None, f"bot 把 {text!r} 收成 {value!r}——負號被吃掉了"
    assert err == "need 0 ≤ lo ≤ hi", (text, err)


_PAIR_RANGE_SAMPLES = [
    ("3-5", [3, 5]),
    ("3 - 5", [3, 5]),
    ("3- 5", [3, 5]),
    ("3,5", [3, 5]),
    ("3 5", [3, 5]),
    ("1e-3,5", [0.001, 5]),
    ("1e-3 1e-2", [0.001, 0.01]),
    ("1e-3-5", [0.001, 5]),
    ("0,0", [0, 0]),
    # 原本就收的寬容寫法，刻意保留：多打的逗號不會改變數字的意思。
    ("3,5,", [3, 5]),
    (",3,5", [3, 5]),
    ("3,,5", [3, 5]),
]


@pytest.mark.parametrize("text, expected", _PAIR_RANGE_SAMPLES)
def test_the_bot_parses_a_range_into_the_numbers_the_user_typed(
        text, expected, tmp_path, monkeypatch):
    """區間的各種寫法照收，而且存進去、讀回來的就是使用者打的那兩個數。"""
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    assert err == "" and value == expected, (text, value, err)
    _write_batch(tmp_path, monkeypatch, "{}")
    applied = bc.save_batch_config({"inter_image_delay_sec": value})
    assert applied["inter_image_delay_sec"] == tuple(float(v) for v in expected)


def test_a_fullwidth_comma_is_still_not_a_separator():
    """維持原本的行為：全形逗號不是分隔符，整串被當成一個數字而擋下（不是存錯）。"""
    import discord_bot as b

    value, err = b._parse_cfg_pair("20，30")
    assert value is None and err.startswith("need two numbers"), (value, err)


_PAIR_EN_DASH_ACCEPTED = [
    ("3–5", [3, 5]),
    ("3 – 5", [3, 5]),
    ("0.5–1.5", [0.5, 1.5]),
    ("1e-3–5", [0.001, 5]),
]


@pytest.mark.parametrize("text, expected", _PAIR_EN_DASH_ACCEPTED + [
    # 打在負號位置的 en dash：意思不明，一律擋下，絕不能被當成分隔符默默丟掉。
    ("–3,5", None),
    ("3,5–", None),
    ("3 –5", None),     # 只有這一格分得出「`–` 會不會被當成正負號」：拿掉那一半，
                        # 它會變成中間的分隔符而收成 3–5，其他幾格仍然被別的關卡擋下
    ("3,–5", None),
    ("3––5", None),
    ("–3–5", None),
])
def test_the_bot_reads_an_en_dash_only_as_a_separator_between_two_numbers(
        text, expected):
    """`/config show` 用 en dash 顯示區間，所以照抄回來要設得進去；但只限夾在兩個數字
    之間——落在負號位置的 en dash 不可以讓一個負數靜靜變成正數。"""
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    if expected is None:
        assert value is None and err, f"bot 把 {text!r} 收成 {value!r}"
    else:
        assert err == "" and value == expected, (text, value, err)


@pytest.mark.parametrize("text", [
    "3,5-", "- 3,5", "--3,5", "3, - 5", "3-- 5", "3,5–",
])
def test_a_dash_that_is_not_between_two_numbers_is_rejected(text):
    """沒有夾在兩個數字之間的破折號，原本會被當成分隔符默默丟掉（`- 3,5` 收成 3–5）。

    比對錯誤訊息本身，證明是「位置」那道關卡擋下的，而不是碰巧解不開。
    """
    import discord_bot as b

    value, err = b._parse_cfg_pair(text)
    assert value is None, f"bot 把 {text!r} 收成 {value!r}"
    assert err == "a dash (- or –) is only allowed between the two numbers", (text, err)


def _normalised_pair(v) -> list:
    return [int(x) if float(x).is_integer() else float(x) for x in v]


def test_what_config_show_displays_for_a_pair_parses_back_to_the_same_value():
    """`/config show` 印出來的區間，原封不動貼回 `/config set` 必須得到同一個值。

    範圍只有區間鍵：樣本取自上面兩支區間測試的預期值、兩個區間鍵的載入器預設值，外加
    兩個會被顯示成指數寫法的值（`1e-05` 這種顯示裡的 `-` 與分隔用的 `–` 挨在一起）。
    比對連型別一起比，所以「讀回來變成 float」也算不一致。
    """
    import discord_bot as b

    pair_keys = {k for k in b._BATCH_SETTERS if bc._COERCERS[k][0] is bc._coerce_pair}
    assert len(pair_keys) >= 2, f"只推出 {sorted(pair_keys)}——`_COERCERS` 的形狀變了？"
    values = [expected for _text, expected in _PAIR_RANGE_SAMPLES + _PAIR_EN_DASH_ACCEPTED]
    values += [bc._DEFAULT_BATCH_CONFIG[k] for k in sorted(pair_keys)]
    values += [(1e-05, 2.5e-05), (0.1, 1e20)]

    for v in values:
        shown = b._fmt_cfg_value(v)
        parsed, err = b._parse_cfg_pair(shown)
        want = _normalised_pair(v)
        assert err == "", f"{v!r} 顯示成 {shown!r}，貼回去卻被擋下：{err}"
        assert [(type(x), x) for x in parsed] == [(type(x), x) for x in want], (
            f"{v!r} 顯示成 {shown!r}，貼回去變成 {parsed!r}")


@pytest.mark.parametrize("key, text", [
    ("images_per_character", _HUGE_INT_TEXT),
    ("inter_image_delay_sec", "-3,5"),
])
def test_config_set_writes_nothing_for_a_value_the_loader_would_not_keep(
        key, text, tmp_path, monkeypatch):
    """走真的 `cmd_config_set`：回覆是拒絕，而且檔案一個字都沒動。"""
    import discord_bot as b

    path = _write_batch(tmp_path, monkeypatch, "{}")
    sent: list = []

    async def _reply(_message, content=None, **_kw):
        sent.append(str(content))

    monkeypatch.setattr(b, "safe_reply", _reply)
    asyncio.run(b.cmd_config_set(types.SimpleNamespace(author=None), f"{key} {text}"))
    assert len(sent) == 1 and sent[0].startswith(f"invalid value for `{key}`"), sent
    assert _HUGE_INT_TEXT not in sent[0], "回覆把使用者打的整串原樣送回去了"
    assert path.read_text(encoding="utf-8") == "{}", "被拒絕的值還是寫進了設定檔"


# ---------------------------------------------------------------------------
# 「設定檔壞掉」那條退路本身的形狀
#
# 2026-09-05 量覆蓋率時發現：兩個載入器的 `except` 分支**一行都沒有被執行過**。
# 那幾行正是 CLAUDE.md 點名的靜默錯誤來源——半截的 `batch_config.json` 會讓載入器
# 退回**整份**預設值（不是只退回被編輯的那個鍵），而使用者收到的是「設定成功」。
# 平常永遠走不到，所以只能靠測試去走。
#
# `_bot_config` 那邊當時還有兩個實際的缺陷，都是「只在設定檔壞掉時才看得到」：
#
# 1. 同一份預設 dict 字面值在三個 `except` 分支各抄了一份。加一個巢狀預設鍵要記得
#    改三處，抄漏一處不會有任何測試變紅。
# 2. `dict(_DEFAULT_USER_ROLES)` 是淺拷貝，三份 id 清單與模組常數是**同一個物件**。
#    呼叫端 append 一次就永久污染預設值，而 `_roles_configured()` 正是用「三份清單
#    都空」判定角色系統沒設定——污染它等於讓權限閘門憑空變成「已設定」。
# ---------------------------------------------------------------------------

def _bot_cfg_at(tmp_path, monkeypatch, payload, name: str):
    """把 `BOT_CONFIG_FILE` 指到 `tmp_path/name`，`payload` 為 None 代表不建檔。"""
    path = tmp_path / name
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", path)
    return bo.load_bot_config()


def _batch_cfg_at(tmp_path, monkeypatch, payload, name: str):
    path = tmp_path / name
    if payload is not None:
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", path)
    return bc.load_batch_config()


# `payload=None` ＝ 檔案不存在（FileNotFoundError）；指到一個**目錄**會踩到
# 另一個 OSError（Windows 上是 PermissionError、POSIX 上是 IsADirectoryError），
# 這是分開走 `except FileNotFoundError` 與 `except OSError` 兩條分支的唯一辦法。
_BROKEN_INPUTS = [
    ("missing", None),
    ("truncated", '{"channel_id": 1,'),
    ("not-a-dict", "[1, 2, 3]"),
    ("empty", ""),
]


@pytest.mark.parametrize("label,payload", _BROKEN_INPUTS)
def test_a_broken_bot_config_still_yields_the_full_shape(
        label, payload, tmp_path, monkeypatch):
    """壞掉的 `bot_config.json` 必須回**與正常檔案一模一樣**的那份設定。

    少一個鍵的後果不是「少一個設定」，是呼叫端在設定檔剛壞掉的那一刻吃到
    `KeyError`——bot 連起都起不來，而這條退路存在的全部理由就是不要在那一刻再壞
    第二次。整份比對（不是只比鍵的集合）才擋得住「鍵在、值變了」那一半。
    """
    baseline = _bot_cfg_at(tmp_path, monkeypatch, "{}", "ok.json")
    broken = _bot_cfg_at(tmp_path, monkeypatch, payload, label + ".json")
    assert broken == baseline, (
        "`bot_config.json` 是 " + label + " 時退回的預設值與正常路徑不一致——"
        "有分支的預設抄漏了。")


def test_an_unreadable_bot_config_still_yields_the_full_shape(
        tmp_path, monkeypatch):
    """`except OSError` 那條分支（不是 FileNotFoundError）也要回同一份。"""
    baseline = _bot_cfg_at(tmp_path, monkeypatch, "{}", "ok.json")
    a_directory = tmp_path / "as_a_dir.json"
    a_directory.mkdir()
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", a_directory)
    assert bo.load_bot_config() == baseline


@pytest.mark.parametrize("label,payload", _BROKEN_INPUTS)
def test_a_broken_batch_config_still_yields_the_full_shape(
        label, payload, tmp_path, monkeypatch):
    """`batch_config.json` 同上——這一份是 CLAUDE.md 明文點名的那個。"""
    baseline = _batch_cfg_at(tmp_path, monkeypatch, "{}", "ok.json")
    broken = _batch_cfg_at(tmp_path, monkeypatch, payload, label + ".json")
    assert broken == baseline


def test_an_unreadable_batch_config_still_yields_the_full_shape(
        tmp_path, monkeypatch):
    baseline = _batch_cfg_at(tmp_path, monkeypatch, "{}", "ok.json")
    a_directory = tmp_path / "as_a_dir.json"
    a_directory.mkdir()
    monkeypatch.setattr(bc, "BATCH_CONFIG_FILE", a_directory)
    assert bc.load_batch_config() == baseline


def _shared_with_module_defaults(cfg, module):
    """`cfg` 裡有哪些可變容器**就是**模組層預設常數本身（同一個物件）。

    比對的是 `id()` 而不是內容：內容當然相等，那正是這個缺陷看不出來的原因。
    """
    owned = {}

    def index(node, path):
        if isinstance(node, (dict, list)):
            owned[id(node)] = path
            items = node.items() if isinstance(node, dict) else enumerate(node)
            for key, value in items:
                index(value, path + "[" + repr(key) + "]")

    for name in dir(module):
        if name.startswith("_DEFAULT"):
            index(getattr(module, name), name)

    hits = []

    def walk(node, path):
        if isinstance(node, (dict, list)):
            if id(node) in owned:
                hits.append((path, owned[id(node)]))
            items = node.items() if isinstance(node, dict) else enumerate(node)
            for key, value in items:
                walk(value, path + "[" + repr(key) + "]")

    walk(cfg, "cfg")
    return hits


@pytest.mark.parametrize("label,payload", _BROKEN_INPUTS)
def test_a_fallback_config_shares_no_mutable_state_with_the_defaults(
        label, payload, tmp_path, monkeypatch):
    """退回預設時拿到的必須是**副本**，不是模組常數本身。

    共用一個 list 的話，呼叫端一次 `append` 就改掉了「預設值」的定義，而且是行程
    活多久就髒多久——重新載入也救不回來，因為髒掉的正是被拿來當基準的那一份。
    """
    cfg = _bot_cfg_at(tmp_path, monkeypatch, payload, label + ".json")
    shared = _shared_with_module_defaults(cfg, bo)
    assert not shared, (
        "退回的設定與模組層預設共用可變物件："
        + "、".join(where + " 就是 " + who for where, who in shared))


def test_mutating_a_fallback_config_leaves_the_defaults_alone(
        tmp_path, monkeypatch):
    """上一支的行為版：真的去改，再確認預設值沒被改到。

    身分比對抓得到「同一個物件」，抓不到「拷貝得不夠深」的其他寫法；這一支反過來
    只看後果，所以換一種實作也還是守得住。
    """
    before = copy.deepcopy(bo._DEFAULT_BOT_CONFIG)
    cfg = _bot_cfg_at(tmp_path, monkeypatch, None, "gone.json")

    touched = 0

    def scribble(node):
        nonlocal touched
        if isinstance(node, dict):
            for value in node.values():
                scribble(value)
        elif isinstance(node, list):
            node.append("__probe__")
            touched += 1

    scribble(cfg)
    assert touched, "設定裡一個 list 都沒有，這支測試等於沒測到東西"
    assert bo._DEFAULT_BOT_CONFIG == before, (
        "改了退回來的設定之後，模組層的預設值跟著變了")


def test_the_broken_paths_do_not_rebuild_the_defaults_by_hand():
    """`load_bot_config` 的每個 `except` 都必須回一個**呼叫**，不是 dict 字面值。

    原本三個分支各抄一份同樣的字面值。抄漏一處只在設定檔壞掉時才看得出來，
    所以這裡直接把「不要再抄第四份」寫成測試。
    """
    tree = ast.parse(Path(bo.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "load_bot_config")
    literals = [
        node.lineno
        for handler in [h for n in ast.walk(func)
                        if isinstance(n, ast.Try) for h in n.handlers]
        for node in ast.walk(handler)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert not literals, (
        "第 " + repr(literals) + " 行又用 dict 字面值重建了一次預設設定——"
        "請改呼叫 `_fallback_bot_config()`，否則下一個巢狀預設鍵會漏掉這裡。")


# ---------------------------------------------------------------------------
# `/config reset` 走的那條路（`reset_batch_config_keys`）——同樣一行都沒被跑過
# ---------------------------------------------------------------------------

def test_resetting_a_key_removes_only_that_key(tmp_path, monkeypatch):
    path = _write_batch(tmp_path, monkeypatch,
                        '{"images_per_character": 7, "rest_hours": 3}')
    cfg, removed = bc.reset_batch_config_keys(["images_per_character"])
    assert removed == ["images_per_character"]
    assert cfg["images_per_character"] == \
        bc._DEFAULT_BATCH_CONFIG["images_per_character"]
    assert cfg["rest_hours"] == 3, "只該清掉指名的那一個鍵"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"rest_hours": 3}, (
        "被清掉的鍵應該從檔案裡**消失**（才會沿用內建預設），"
        "而不是被寫成預設值的字面量")


def test_resetting_a_key_that_was_never_set_writes_nothing(
        tmp_path, monkeypatch):
    """沒東西可清就不要動檔案——多寫一次會把手動編輯的排版沖掉。"""
    path = _write_batch(tmp_path, monkeypatch, '{"rest_hours": 3}')
    original = path.read_bytes()
    cfg, removed = bc.reset_batch_config_keys(["images_per_character"])
    assert removed == []
    assert cfg["rest_hours"] == 3
    assert path.read_bytes() == original


def test_a_failed_replace_cleans_up_its_temp_file(tmp_path, monkeypatch):
    """`os.replace` 失敗時要把 temp 收掉再把錯誤丟回去。

    留下來的話下一次寫入會踩到別人的殘骸；吞掉的話呼叫端會以為寫成功了。
    """
    path = _write_batch(tmp_path, monkeypatch, "{}")
    tmp_file = tmp_path / "batch_config.json.tmp"

    def boom(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        bc.save_batch_config({"rest_hours": 3})
    assert not tmp_file.exists(), "寫入失敗卻留下了 temp 檔"
    assert json.loads(path.read_text(encoding="utf-8")) == {}, "原檔不該被動到"


# ===========================================================================
# 被拒絕的鍵一定要出聲（2026-09-09）
# ===========================================================================
# 這個模組的 docstring 從第一版就寫著「Keys present but wrong type / out of range
# → use the per-key default and print a warning」。**在這一輪之前那句話是假的**：
# 十五個鍵裡只有 `model_candidates` 真的印，其餘十四個靜默退回。
#
# 靜默退回的症狀是「我改了設定卻沒有生效」，而使用者手上沒有任何線索——`/config
# show` 顯示的是**生效值**，看起來一切正常。（Discord 那條路徑本來就安全：
# `cmd_config_set` 會把存檔後**讀回來的值**回報成 `舊 → 新`，所以被打回去會直接
# 顯示成 `28 → 28`。會踩到的是手動編輯 `batch_config.json` 的人，而那正是這個檔
# 支援熱重載的理由。）

def _cfg_with(tmp_path, monkeypatch, payload: dict):
    _write_batch(tmp_path, monkeypatch, json.dumps(payload))
    return bc.load_batch_config()


def test_a_rejected_key_says_so(tmp_path, monkeypatch, capsys):
    cfg = _cfg_with(tmp_path, monkeypatch, {"images_per_character": "8"})
    assert cfg["images_per_character"] == \
        bc._DEFAULT_BATCH_CONFIG["images_per_character"]
    err = capsys.readouterr().err
    assert "images_per_character" in err, f"沒說是哪個鍵：{err!r}"
    assert "'8'" in err, f"沒說收到什麼值，換一種錯法時分不出來：{err!r}"


# --------------------------------------------------------------------------
# 鍵名打錯
#
# 2026-09-09 早上把兩個模組的**值**驗證補齊了（型別不對會 `_warn_once`），但
# **鍵名打錯**仍然完全靜默：載入器只走自己認得的鍵，`raw` 裡多出來的東西連讀都
# 沒讀到。對使用者來說兩者的症狀**一模一樣**——設定沒生效；而這兩個檔案本來就要
# 重啟才生效，所以他重啟完只會以為生效了。
# --------------------------------------------------------------------------
def test_a_misspelled_batch_key_says_so(tmp_path, monkeypatch, capsys):
    """打錯的鍵名要被點名，而且正確的那個鍵**照樣生效**。

    後半句是正面對照組：少了它，「一律當成壞掉、整份退回預設」也會讓前半句通過。
    """
    cfg = _cfg_with(tmp_path, monkeypatch,
                    {"images_per_charater": 3, "images_per_character": 7})
    err = capsys.readouterr().err
    assert "images_per_charater" in err, f"沒點名打錯的鍵：{err!r}"
    assert cfg["images_per_character"] == 7, "拼對的那個鍵沒生效"


def test_a_misspelled_bot_key_says_so(tmp_path, monkeypatch, capsys):
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"alert_user_ids": 123, "alert_user_id": 456})
    err = capsys.readouterr().err
    assert "alert_user_ids" in err, f"沒點名打錯的鍵：{err!r}"
    assert cfg["alert_user_id"] == 456, "拼對的那個鍵沒生效"


def test_a_correct_config_says_nothing_about_unknown_keys(tmp_path, monkeypatch,
                                                          capsys):
    """**每一輪都響的警告等於沒有警告。** 全對的設定必須完全安靜。

    `load_bot_config()` 每一次對外 HTTP 請求都會被呼叫一次，所以這一支不只是
    潔癖——會響的話那就是下一個 `rpc apply ->`（那份 log 曾經 95.8% 是同一句話）。
    """
    _cfg_with(tmp_path, monkeypatch, {"images_per_character": 7})
    assert "不認得" not in capsys.readouterr().err
    _bot_cfg_with(tmp_path, monkeypatch, {"alert_user_id": 456})
    assert "不認得" not in capsys.readouterr().err


def test_json_comment_keys_are_not_treated_as_unknown(tmp_path, monkeypatch,
                                                      capsys):
    """`_` 開頭是本專案在 JSON 裡寫註解的慣例——`bot_config.json` 現在有 16 個。

    把它們算成未知鍵的話，正式設定檔一啟動就吐 16 個名字，而那正是上一支在防的
    事：一個永遠會響的警告會被人關掉。
    """
    _bot_cfg_with(tmp_path, monkeypatch,
                  {"_alert_user_id_comment": "說明文字", "alert_user_id": 1})
    assert "不認得" not in capsys.readouterr().err


def test_the_unknown_key_warning_prints_names_but_never_values(
        tmp_path, monkeypatch, capsys):
    """訊息只帶鍵名。值可能是使用者填的主機路徑，而 stderr 會進 log，
    `/log tail` 會把 log 送到對話平台——跟 `_shown()` 對容器只印型別名同一條理由。
    """
    secret = "D:/some/host/path/that/must/not/leak"
    _bot_cfg_with(tmp_path, monkeypatch, {"totally_made_up_key": secret})
    err = capsys.readouterr().err
    assert "totally_made_up_key" in err
    assert secret not in err, f"把值也印出去了：{err!r}"


def test_the_unknown_key_warning_is_capped(tmp_path, monkeypatch, capsys):
    """一個貼壞的 JSON 不該把整份記錄洗掉：最多列 8 個，其餘只報數量。"""
    payload = {f"bogus_key_{i}": i for i in range(30)}
    _bot_cfg_with(tmp_path, monkeypatch, payload)
    err = capsys.readouterr().err
    listed = sum(1 for i in range(30) if f"bogus_key_{i}" in err)
    assert listed == 8, f"列了 {listed} 個，應該剛好 8 個：{err!r}"
    assert "另有 22 個" in err, f"沒報剩下幾個：{err!r}"


def test_the_unknown_key_scan_returns_what_it_found():
    """比對器自己的對照組：拿合成資料確認兩個方向都對。

    現況（正式的兩個設定檔）零未知鍵，所以上面那幾支主測試在真實資料上本來就
    不會紅——牙齒要長在這裡。
    """
    known = {"a": 1, "b": 2}
    assert bo._warn_unknown_keys({"a": 1}, known, source="t") == []
    assert bo._warn_unknown_keys({"c": 1}, known, source="t") == ["c"]
    # `_` 開頭是註解慣例，不算未知
    assert bo._warn_unknown_keys({"_a_comment": "x"}, known, source="t") == []
    # 排序、去重複的形狀
    assert bo._warn_unknown_keys({"z": 1, "y": 2}, known, source="t") == ["y", "z"]


def test_the_two_loaders_use_the_same_unknown_key_rule():
    """兩個模組各有一份 `_warn_unknown_keys`（跟三份 `_warn_once` 同樣的重複）。

    重複本身是刻意的（被動模組彼此不互相 import），但**規則**必須一致，否則
    「哪個設定檔會抓到我的錯字」變成要看運氣。這裡拿同一組輸入問兩邊。
    """
    known = {"a": 1}
    for case in ({"a": 1}, {"b": 2}, {"_c": 3}, {"b": 1, "d": 2}):
        assert (bo._warn_unknown_keys(case, known, source="bo")
                == bc._warn_unknown_keys(case, known, source="bc")), case


def test_an_accepted_key_is_silent_and_actually_applied(tmp_path, monkeypatch,
                                                        capsys):
    """**正面對照組。** 少了它，「一律警告並退回預設」也會讓上面那支通過——
    而那會讓每一個正常的設定都失效，比原本的缺陷嚴重得多。"""
    cfg = _cfg_with(tmp_path, monkeypatch, {"images_per_character": 9})
    assert cfg["images_per_character"] == 9
    assert capsys.readouterr().err == ""


def test_a_key_that_is_simply_absent_is_silent(tmp_path, monkeypatch, capsys):
    """沒寫的鍵是**正常情況**，一個字都不該印——這條路每個角色都會走。"""
    cfg = _cfg_with(tmp_path, monkeypatch, {})
    assert cfg["rest_hours"] == bc._DEFAULT_BATCH_CONFIG["rest_hours"]
    assert capsys.readouterr().err == ""


def test_a_key_written_as_null_is_not_mistaken_for_an_absent_one(
        tmp_path, monkeypatch, capsys):
    """`null` **不是**「沒寫」，是使用者真的打錯了，要出聲。

    `_take` 刻意先問 `key not in raw` 而不是 `raw.get(key)`，理由就寫在它自己的
    docstring 裡——而在這支測試出現之前，**沒有任何東西在執行那句話**（2026-09-09
    變異測試抓到：改成看起來更順眼的 `raw.get(key) is None`，兩個模組全綠）。那個
    改法會讓 `null` 跟沒寫的鍵長得一模一樣、安靜退回預設，正好是這一整輪在修的
    缺陷本身。

    這裡沒有「`null` 其實是合法值」的例外：兩份預設設定都沒有任何一個鍵的預設是
    `None`（連巢狀區段都沒有，見 `_no_none_defaults`），所以設定檔裡的 `null`
    一律是錯的。
    """
    assert _no_none_defaults(bc._DEFAULT_BATCH_CONFIG)      # 前提
    cfg = _cfg_with(tmp_path, monkeypatch, {"images_per_character": None})
    assert cfg["images_per_character"] == \
        bc._DEFAULT_BATCH_CONFIG["images_per_character"]
    assert "images_per_character" in capsys.readouterr().err


def _no_none_defaults(defaults: dict) -> bool:
    """預設設定裡（含巢狀區段）沒有任何一個鍵的預設值是 `None`。

    上面那條規則的前提。哪天真的有一個鍵想用 `None` 當「未設定」，這個前提會先
    變紅，提醒把那個鍵排除掉，而不是讓 `null` 的警告變成假警告。
    """
    return all(_no_none_defaults(v) if isinstance(v, dict) else v is not None
               for v in defaults.values())


def test_a_value_equal_to_the_default_is_not_reported_as_rejected(
        tmp_path, monkeypatch, capsys):
    """**判定必須是「被拒絕」，不是「結果等於預設」。**

    這正是不用「比對值」而用 sentinel 的理由：使用者明確寫下一個剛好等於預設的
    值，是完全正當的，不該被指控成設定沒生效。
    """
    default = bc._DEFAULT_BATCH_CONFIG["rest_hours"]
    cfg = _cfg_with(tmp_path, monkeypatch, {"rest_hours": default})
    assert cfg["rest_hours"] == default
    assert capsys.readouterr().err == ""


def test_a_normalised_value_is_not_reported_as_rejected(tmp_path, monkeypatch,
                                                        capsys):
    """`[1, 2]` → `(1.0, 2.0)`、`" a "` → `"a"` 都是**正當的正規化**，不是拒絕。

    拿值去比對的實作會把這些全部誤報成「你的設定被丟掉了」，而一個會亂叫的守門
    遲早被人關掉——本專案已經為了同一個理由收窄過兩支靜態掃描的範圍。
    """
    cfg = _cfg_with(tmp_path, monkeypatch, {
        "inter_image_delay_sec": [1, 2],
        "model_candidates": ["  NAI Diffusion V5 Full  "]})
    assert cfg["inter_image_delay_sec"] == (1.0, 2.0)
    assert cfg["model_candidates"] == ("NAI Diffusion V5 Full",)
    assert capsys.readouterr().err == ""


def test_every_default_key_goes_through_a_coercer(tmp_path, monkeypatch):
    """表要涵蓋每一個預設鍵，兩個方向都釘。

    少了這支，新增一個設定卻忘了掛 coercion 就會安靜地完全不驗證；反方向的多餘
    項目則會在 `_take` 裡變成 `KeyError`。
    """
    assert set(bc._COERCERS) == set(bc._DEFAULT_BATCH_CONFIG), (
        "`_COERCERS` 與 `_DEFAULT_BATCH_CONFIG` 的鍵集合對不上："
        f"少了 {set(bc._DEFAULT_BATCH_CONFIG) - set(bc._COERCERS)}，"
        f"多了 {set(bc._COERCERS) - set(bc._DEFAULT_BATCH_CONFIG)}")


def test_the_same_complaint_is_not_repeated_every_reload(tmp_path, monkeypatch,
                                                         capsys):
    """`load_batch_config()` 沒有快取——webrunner 每個角色、bot 每次查詢都重讀。

    一個放著沒改的錯字若每次都印，就會變成下一個把有用診斷擠出記錄檔的雜訊源。
    """
    _write_batch(tmp_path, monkeypatch, json.dumps({"rest_hours": "nope"}))
    for _ in range(5):
        bc.load_batch_config()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"同一段警告印了 {len(lines)} 次：{lines}"


def test_a_different_bad_key_still_gets_through(tmp_path, monkeypatch, capsys):
    """去重的鍵是完整訊息，所以第二種錯法要看得到——否則第一個錯字會把之後所有
    診斷永久靜音。"""
    _write_batch(tmp_path, monkeypatch, json.dumps({"rest_hours": "nope"}))
    bc.load_batch_config()
    _write_batch(tmp_path, monkeypatch,
                 json.dumps({"min_save_ratio": "nope"}))
    bc.load_batch_config()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 2, f"第二種錯法被吃掉了：{lines}"


# ===========================================================================
# `_bot_config` 被拒絕／被夾的鍵一定要出聲（2026-09-09）
# ===========================================================================
# 這個模組的 docstring 從第一版就寫著「Wrong-type values fall back to the per-key
# default with a stderr warning」。**在這一輪之前那句話是假的**：整份檔案只有兩個
# `print`，兩個都是**整份檔案**讀不到／解不開，逐鍵的警告一個都沒有。
#
# 實測（壞掉的設定檔跑一次 `load_bot_config()`）：六個鍵被拒、一個被夾，stderr
# **一個字都沒有**。而 `bot_config.json` 的變更本來就需要重啟才生效，所以使用者
# 重啟了、以為設定生效了，其實跑的是預設值。
#
# 這一組的形狀刻意與上面 `_batch_config` 那一組對齊（同一個 sentinel 判定、同一批
# 反面對照組），因為兩支載入器犯的是同一個錯。

def _bot_cfg_with(tmp_path, monkeypatch, payload: dict, name="bot.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(bo, "BOT_CONFIG_FILE", path)
    return bo.load_bot_config()


def test_a_rejected_bot_key_says_so(tmp_path, monkeypatch, capsys):
    """實際踩到的形狀：把秒數寫成字串。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"event_poll_seconds": "3"})
    assert cfg["event_poll_seconds"] == \
        bo._DEFAULT_BOT_CONFIG["event_poll_seconds"]
    err = capsys.readouterr().err
    assert "event_poll_seconds" in err, f"沒說是哪個鍵：{err!r}"
    assert "'3'" in err, f"沒說收到什麼值，換一種錯法時分不出來：{err!r}"


def test_an_accepted_bot_key_is_silent_and_actually_applied(
        tmp_path, monkeypatch, capsys):
    """**正面對照組。** 少了它，「一律警告並退回預設」也會讓上面那支通過——
    而那會讓每一個正常的設定都失效，比原本的缺陷嚴重得多。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"event_poll_seconds": 3.0})
    assert cfg["event_poll_seconds"] == 3.0
    assert capsys.readouterr().err == ""


def test_a_bot_key_that_is_simply_absent_is_silent(tmp_path, monkeypatch,
                                                   capsys):
    """沒寫的鍵是**正常情況**，一個字都不該印。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {})
    assert cfg["event_poll_seconds"] == \
        bo._DEFAULT_BOT_CONFIG["event_poll_seconds"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("key", [
    "alert_user_id",              # 走 `_take`
    "path_reveal_channel_ids",    # 走 `_take_int_list`
    "daily_health_report",        # 走 `_section`
])
def test_a_bot_key_written_as_null_is_not_mistaken_for_an_absent_one(
        tmp_path, monkeypatch, capsys, key):
    """`null` 不是「沒寫」。理由與批次那一側同一條（見
    `test_a_key_written_as_null_is_not_mistaken_for_an_absent_one`），但這裡的
    入口有**三個**——`_take`／`_take_int_list`／`_section` 各自寫了一次
    `key not in raw`，三份都可以各自被「簡化」成 `raw.get(key)`，所以三條都要釘。
    """
    assert _no_none_defaults(bo._DEFAULT_BOT_CONFIG)        # 前提
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {key: None})
    assert cfg[key] == bo._DEFAULT_BOT_CONFIG[key]
    assert key in capsys.readouterr().err


def test_a_bot_value_equal_to_the_default_is_not_reported_as_rejected(
        tmp_path, monkeypatch, capsys):
    """使用者明確寫下一個剛好等於預設的值，不該被指控成設定沒生效。

    這是「結果 == 預設 ⇒ 被拒絕」那種實作的反例。
    """
    default = bo._DEFAULT_BOT_CONFIG["min_free_disk_gb"]
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"min_free_disk_gb": default})
    assert cfg["min_free_disk_gb"] == default
    assert capsys.readouterr().err == ""


def test_a_normalised_bot_value_is_not_reported_as_rejected(
        tmp_path, monkeypatch, capsys):
    """`" FULL "` → `"full"`、`" ZH-TW "` → `"zh-tw"` 都是**正當的正規化**。

    這是「結果 != 輸入 ⇒ 被拒絕」那種實作的反例。拿值去比會把這些全部誤報成
    「你的設定被丟掉了」，而一個會亂叫的守門遲早被人關掉。
    """
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {
        "dorossi_cc_tools": " FULL ", "default_help_lang": " ZH-TW "})
    assert cfg["dorossi_cc_tools"] == "full"
    assert cfg["default_help_lang"] == "zh-tw"
    assert capsys.readouterr().err == ""


def test_a_rejected_value_that_happens_to_equal_the_default_still_says_so(
        tmp_path, monkeypatch, capsys):
    """**這就是用 sentinel 而不是比對值的理由。**

    `alert_user_id: false` 是型別錯（`bool` 是 `int` 的子類，`_coerce_int` 擋掉），
    但它退回的預設是 `0`，而 `False == 0` 為真——「結果 == 輸入 ⇒ 接受了」那種實作
    會安靜放過它。sentinel 問的是「coercer 有沒有把這個值丟掉」，跟值長什麼樣無關。
    """
    assert bo._DEFAULT_BOT_CONFIG["alert_user_id"] == 0  # 前提：預設剛好是 0
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"alert_user_id": False})
    assert cfg["alert_user_id"] == 0
    assert "alert_user_id" in capsys.readouterr().err


def test_a_clamped_bot_value_says_so_in_words_of_its_own(
        tmp_path, monkeypatch, capsys):
    """**夾（clamp）不是拒絕，要說、而且要說得不一樣。**

    使用者明確寫了 10、實際跑 60，那不是正規化，是他的意圖被一個不開放關掉的保護
    下限改掉了——他有權知道，但那跟「值不合用」是兩件事。
    """
    floor = bo.DOROSSI_CC_HARD_LIMIT_FLOOR_SEC
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"dorossi_cc_hard_limit_off_sec": 10})
    assert cfg["dorossi_cc_hard_limit_off_sec"] == floor
    err = capsys.readouterr().err
    assert "dorossi_cc_hard_limit_off_sec" in err
    assert "不合用" not in err, f"夾被講成了拒絕：{err!r}"


def test_a_value_above_the_floor_is_not_reported_as_clamped(
        tmp_path, monkeypatch, capsys):
    """夾的反面對照組——沒被夾就不該出聲。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"dorossi_cc_hard_limit_off_sec": 1200})
    assert cfg["dorossi_cc_hard_limit_off_sec"] == 1200.0
    assert capsys.readouterr().err == ""


def test_a_huge_but_legal_number_is_not_reported_as_clamped(
        tmp_path, monkeypatch, capsys):
    """`float(2**53 + 1) != 2**53 + 1`——拿「結果 != 輸入」判斷有沒有被夾的實作
    會把這個完全合法（遠高於下限）的整數誤報成被夾。

    判定改成「再問同一個 coercer 一次、只把下限拿掉」，所以不受浮點精度影響。
    """
    huge = 2 ** 53 + 1
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"dorossi_cc_hard_limit_off_sec": huge})
    assert cfg["dorossi_cc_hard_limit_off_sec"] == float(huge)
    assert capsys.readouterr().err == ""


def test_writing_the_defaults_verbatim_is_silent(tmp_path, monkeypatch, capsys):
    """**任何鍵的預設值本身都不得被它自己的 coercer 拒絕。**

    否則就會有一個「照著預設寫也挨罵」的假警告，而使用者無從修正——那正是會讓人
    把守門關掉的那種噪音。這支不是理論：2026-09-09 加警告時它當場抓到
    `api_contact`（預設 `""`，而 `_coerce_str` 把空字串當拒絕），正式的
    `bot_config.json` 就寫著那個值，於是每一次載入都會叫一次。
    """
    cfg = _bot_cfg_with(tmp_path, monkeypatch, dict(bo._DEFAULT_BOT_CONFIG))
    assert cfg == bo.load_bot_config()
    err = capsys.readouterr().err
    assert err == "", f"照著預設寫卻收到警告，那個鍵的 coercer 拒絕了自己的預設：{err!r}"


def test_a_wrong_type_section_says_so(tmp_path, monkeypatch, capsys):
    """整個區段型別錯 → 那一段**全部**的設定安靜蒸發。原本連一個字都沒有。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"dashboard": 8765})
    assert cfg["dashboard"] == bo._DEFAULT_DASHBOARD
    assert "dashboard" in capsys.readouterr().err


def test_a_field_inside_a_section_says_so(tmp_path, monkeypatch, capsys):
    """`daily_health_report.channel_id` 寫成字串是最像得到的打字錯——聊天平台的
    「複製 ID」給的就是一串數字，貼進 JSON 時很容易連引號一起帶上。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"daily_health_report": {"channel_id": "12345"}})
    assert cfg["daily_health_report"]["channel_id"] == 0
    err = capsys.readouterr().err
    assert "daily_health_report.channel_id" in err, \
        f"訊息要指得出是哪個區段的哪個欄位：{err!r}"


def test_a_section_field_that_is_absent_is_silent(tmp_path, monkeypatch,
                                                  capsys):
    """區段裡沒寫的欄位一樣是正常情況。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"daily_health_report": {"enabled": True}})
    assert cfg["daily_health_report"]["enabled"] is True
    assert cfg["daily_health_report"]["time"] == "09:00"
    assert capsys.readouterr().err == ""


def test_an_id_list_that_is_not_a_list_says_so(tmp_path, monkeypatch, capsys):
    """`path_reveal_channel_ids` 型別錯 → 空清單。方向是安全的（退回私訊限定），
    但**fail-closed 的失敗正是最不會有人發現的那一種**：使用者以為自己開了那個
    頻道的路徑顯示，實際上沒有，而且沒有任何訊號。"""
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"path_reveal_channel_ids": 123})
    assert cfg["path_reveal_channel_ids"] == []
    assert "path_reveal_channel_ids" in capsys.readouterr().err


def test_dropped_id_entries_say_how_many_but_not_which(
        tmp_path, monkeypatch, capsys):
    """逐筆丟掉的項目要報**數量**，不要報內容。

    這幾個鍵裝的是使用者／頻道 ID，逐筆列出只是雜訊；而更一般的規則是——警告會進
    記錄檔，而記錄檔有一條使用者面的出口（`/log tail`），所以能不外送的內容就不要
    放進去。
    """
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"user_roles": {"admin_user_ids": [111, "nope", -2]}})
    assert cfg["user_roles"]["admin_user_ids"] == [111]
    err = capsys.readouterr().err
    assert "user_roles.admin_user_ids" in err
    assert "2" in err, f"沒說丟了幾筆：{err!r}"
    assert "111" not in err, f"警告裡出現了 ID 內容：{err!r}"


def test_a_clean_id_list_is_silent(tmp_path, monkeypatch, capsys):
    cfg = _bot_cfg_with(tmp_path, monkeypatch,
                        {"path_reveal_channel_ids": [1, "2"]})
    assert cfg["path_reveal_channel_ids"] == [1, 2]
    assert capsys.readouterr().err == ""


def test_comment_keys_in_launch_aliases_are_not_reported_as_dropped(
        tmp_path, monkeypatch, capsys):
    """底線開頭的 key 是**刻意**跳過的註解，不是壞資料。

    把它們算成「被丟掉」會讓每一個有註解的設定檔都收到一則假警告——同一個模組的
    `_coerce_gui_control` 本來就特地支援 `_comment`，守門不該反過來罵它。
    """
    cfg = _bot_cfg_with(tmp_path, monkeypatch, {"gui_control": {
        "launch_aliases": {"_comment": "說明", "mygame": "steam://rungameid/1"}}})
    assert cfg["gui_control"]["launch_aliases"] == {"mygame": "steam://rungameid/1"}
    assert capsys.readouterr().err == ""


def test_a_container_value_is_reported_by_type_not_by_content(
        tmp_path, monkeypatch, capsys):
    """容器只印型別名。`gui_control.launch_aliases` 裝的是主機路徑與 URI，而
    stderr 會進記錄檔、記錄檔有 `/log tail` 這條出口。那條路徑確實會過
    `_redact_for_discord`，但「安全性靠下游某個 scrubber 才成立」正是本專案一再
    吃虧的形狀——把保證留在本地。"""
    secret = "D:/Work/Example/secret_tool.exe"
    _bot_cfg_with(tmp_path, monkeypatch,
                  {"gui_control": {"launch_whitelist": {"a": secret}}})
    err = capsys.readouterr().err
    assert "launch_whitelist" in err
    assert secret not in err, f"警告裡出現了主機路徑：{err!r}"
    assert "dict" in err, f"至少要說收到的是什麼型別：{err!r}"


def test_every_default_bot_key_is_covered_exactly_once():
    """三張表要**不重不漏**地蓋滿 `_DEFAULT_BOT_CONFIG`，兩個方向都釘。

    少一個鍵不只是「那個鍵不驗證、也不出聲」：`load_bot_config` 從
    `dict(_DEFAULT_BOT_CONFIG)` 出發，漏掉的鍵會讓回傳值留著淺拷貝來的**同一個**
    模組常數容器，呼叫端 append 一次就永久污染預設值（`_fallback_bot_config` 的
    docstring 記著那個缺陷：`_roles_configured()` 會憑空變成「已設定」）。
    反方向的多餘項目則會在 `_take` 裡變成 `KeyError`。
    """
    flat = set(bo._COERCERS)
    sections = set(bo._SECTION_KEYS)
    int_lists = set(bo._INT_LIST_KEYS)
    covered = flat | sections | int_lists
    assert len(flat) + len(sections) + len(int_lists) == len(covered), (
        "同一個鍵被列進兩張表："
        f"{[k for k in covered if (k in flat) + (k in sections) + (k in int_lists) > 1]}")
    assert covered == set(bo._DEFAULT_BOT_CONFIG), (
        f"少了 {set(bo._DEFAULT_BOT_CONFIG) - covered}，"
        f"多了 {covered - set(bo._DEFAULT_BOT_CONFIG)}")


@pytest.mark.parametrize("table,defaults,label", [
    ("_SUPERVISOR_COERCERS", "_DEFAULT_SUPERVISOR", "webrunner_supervisor"),
    ("_DAILY_HEALTH_COERCERS", "_DEFAULT_DAILY_HEALTH_REPORT",
     "daily_health_report"),
    ("_DASHBOARD_COERCERS", "_DEFAULT_DASHBOARD", "dashboard"),
])
def test_every_section_table_covers_its_own_defaults(table, defaults, label):
    """區段的欄位表同樣兩個方向都釘——新增一個區段欄位卻忘了掛 coercion，就是
    「完全不驗證、也不出聲」，正是這一輪在修的那個缺陷。"""
    assert set(getattr(bo, table)) == set(getattr(bo, defaults)), label


def test_every_covered_bot_key_actually_complains(tmp_path, monkeypatch,
                                                  capsys):
    """把**每一個**鍵各餵一個一定不合用的值，逐一確認它會出聲。

    表格式的守門（上面那支）只回答「名字在不在表裡」，回答不了「那張表真的被查了
    嗎」——這個 repo 已經出過 `if False and not decided:` 那種名字在、行為沒了的東
    西。`{}` 對每一個 coercer 都是壞值（九個扁平 helper 逐一驗過），所以可以當統一
    的探針。
    """
    missing = []
    for key in list(bo._COERCERS) + list(bo._INT_LIST_KEYS):
        _warn_dedup._WARNED.clear()   # 去重集合已搬進共用被動模組
        capsys.readouterr()
        _bot_cfg_with(tmp_path, monkeypatch, {key: {}}, name=f"{key}.json")
        if key not in capsys.readouterr().err:
            missing.append(key)
    assert not missing, f"這些鍵被丟掉時一個字都沒印：{missing}"


def test_every_section_complains_when_its_whole_block_is_the_wrong_type(
        tmp_path, monkeypatch, capsys):
    """同上，但針對「整段蒸發」——那是單一鍵裡代價最大的一種。"""
    missing = []
    for key in bo._SECTION_KEYS:
        _warn_dedup._WARNED.clear()   # 去重集合已搬進共用被動模組
        capsys.readouterr()
        _bot_cfg_with(tmp_path, monkeypatch, {key: "nope"}, name=f"s_{key}.json")
        if key not in capsys.readouterr().err:
            missing.append(key)
    assert not missing, f"這些區段整段被丟掉時一個字都沒印：{missing}"


def test_the_same_bot_complaint_is_not_repeated_every_reload(
        tmp_path, monkeypatch, capsys):
    """去重是必要的，不是裝飾。

    模組 docstring 的「Loaded once at import time」只對 `discord_bot.BOT_CONFIG`
    成立：`_external_apis._user_agent()` **每一次對外 HTTP 請求**都會呼叫一次
    `load_bot_config()` 去讀 `api_contact`。沒有去重的話，一個放著沒改的錯字會用
    同一行文字洗掉整份記錄檔——`discord_bot.log` 已經為了另一件事發生過一次
    （96% 的行是同一句話）。
    """
    _bot_cfg_with(tmp_path, monkeypatch, {"event_poll_seconds": "nope"})
    for _ in range(5):
        bo.load_bot_config()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"同一段警告印了 {len(lines)} 次：{lines}"


def test_a_different_bad_bot_key_still_gets_through(tmp_path, monkeypatch,
                                                    capsys):
    """去重的鍵是完整訊息，所以第二種錯法要看得到——否則第一個錯字會把之後所有
    診斷永久靜音。"""
    _bot_cfg_with(tmp_path, monkeypatch, {"event_poll_seconds": "nope"}, "a.json")
    _bot_cfg_with(tmp_path, monkeypatch, {"min_free_disk_gb": "nope"}, "b.json")
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 2, f"第二種錯法被吃掉了：{lines}"


# ---------------------------------------------------------------------------
# 同一條規則的**範圍**：`inf` 不是那兩個載入器專屬的問題
# ---------------------------------------------------------------------------
# 上面那一組守的是「兩個載入器的每個 `_coerce_*` 都擋得掉 inf/nan/超大 int」。
# 那條規則的**判準**跟模組無關——本檔開頭自己寫著「所有檢查都長成
# `isinstance(v, (int, float)) and v > 0`，而 `inf > 0` 為真」——可是範圍被寫死在
# 兩件事上：一份兩個檔名的清單，以及 `_coerce_` 這個命名慣例。
#
# 兩者都漏了東西，而且漏出來的是**真的**。`presence_rpc.json` 一樣是手編的設定
# 檔、一樣每個 probe tick 重讀，而 `discord_rpc.load_rpc_config` 一個 `_coerce_*`
# 都沒有，所以它從這道守門底下整個走過去：`{"refresh_sec": 1e400}` → `inf` →
# `apply()` 的保活節流 `(now - _last_send_ts) < refresh_sec` **恆為真** → 保活
# 再也不會送，而保活的工作正是偵測桌面端已經關掉。而且完全無聲——`apply()` 回
# `unchanged`，`_rpc_health_class` 把它歸類成健康。2026-09-10 修好並補了行為測試。
#
# 判準因此改成機制：一個布林測試把某個名字當成 float 收下（`isinstance` 的第二個
# 引數含 `float`），而那個名字之後真的被當成**量**用（`float()` 或 `< > <= >=`），
# 中間卻沒有問過有限性。只掃「會讀 JSON 設定」的模組——手編的 `1e400` 只從那裡
# 進得來，掃全部只會製造假陽性，而會亂叫的守門遲早被關掉。
_FINITE_TOKENS = frozenset({"isfinite", "isinf", "isnan", "_is_finite_number"})

# 已知且暫時接受的。每一筆都要寫「壞值進來會被誤讀成什麼」。
_FINITENESS_EXEMPT = {
    "_chrome_slot.py:_is_stale":
        "`acquired_at` 是本專案自己寫進 `chrome_slot.lock` 的，不是手編值；而且 "
        "`inf` 只會讓『時間過期』這一條永遠不成立，pid 已死那一條仍然照常判定 "
        "stale，所以槽不會被永久卡住。要收緊是 `_chrome_slot` 那一側的事。",
}

_FINITENESS_MODULE_FLOOR = 8


def _config_reading_modules(pkg_root=None, repo_root=None):
    """會 `json.load` / `json.loads` 的**非測試**專案模組 → `[(檔名, AST)]`。

    用 glob 不用列舉：列舉是 fail-open 的（下一個新載入器不會自動被蓋到）。
    兩個目錄是參數，好讓範圍釘樁餵得進 `tmp_path`——今天的答案剛好對，不代表
    列舉是算出來的（§8.8(A3)）。
    """
    package = PKG_ROOT if pkg_root is None else pkg_root
    root = package.parent if repo_root is None else repo_root
    out = []
    for path in sorted(package.glob("*.py")) + sorted(root.glob("*.py")):
        if path.name.startswith(("test_", "_test_")):
            continue
        if path.name in ("conftest.py", "__init__.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        except (SyntaxError, UnicodeDecodeError):   # pragma: no cover
            continue
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in ("load", "loads")
               and getattr(n.func.value, "id", "") == "json"
               for n in ast.walk(tree)):
            out.append((path.name, tree))
    return out


def _float_admitted_names(test: ast.expr) -> set[str]:
    """這個布林測試靠 `isinstance(x, float)`（或含 float 的 tuple）收下的名字。"""
    out: set[str] = set()
    for node in ast.walk(test):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "isinstance"
                and len(node.args) == 2
                and "float" in _type_names(node.args[1])
                and isinstance(node.args[0], ast.Name)):
            out.add(node.args[0].id)
    return out


def _asks_about_finiteness(test: ast.expr) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name in _FINITE_TOKENS:
                return True
    return False


def _used_as_a_quantity(fn: ast.AST, name: str) -> bool:
    """`name` 後來有沒有被 `float()` 或拿去比大小。

    這一關是把「收下數字只為了 `str()` 它」的格式化 helper 擋在外面的東西——
    那種地方 `inf` 沒有任何危害，抓進來只是雜訊。
    """
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "float"
                and any(isinstance(a, ast.Name) and a.id == name
                        for a in node.args)):
            return True
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                for op in node.ops):
            for operand in [node.left] + list(node.comparators):
                if isinstance(operand, ast.Name) and operand.id == name:
                    return True
    return False


def _finiteness_gaps(trees) -> list[str]:
    """`[(檔名, AST)]` → 「收下當數字用、卻沒問有限性」的位置。

    純函式，好讓合成語料問得到它——真實資料修乾淨之後這支永遠回空集合，而一個
    永遠回空的偵測器跟 `return []` 在輸出上一模一樣。
    """
    out: list[str] = []
    for module_name, tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in _FINITE_TOKENS:      # 述詞自己不算
                continue
            for node in ast.walk(fn):
                test = (node.test if isinstance(node, (ast.If, ast.IfExp))
                        else None)
                if test is None or _asks_about_finiteness(test):
                    continue
                for name in _float_admitted_names(test):
                    if _used_as_a_quantity(fn, name):
                        out.append(f"{module_name}:{fn.name}")
    return sorted(set(out))


def test_no_config_number_is_admitted_without_asking_if_it_is_finite():
    trees = _config_reading_modules()
    assert len(trees) >= _FINITENESS_MODULE_FLOOR, (
        f"只抽到 {len(trees)} 個會讀 JSON 的模組（下限 "
        f"{_FINITENESS_MODULE_FLOOR}）——抽取器壞了。抽不到檔案時「零筆違規」跟"
        "「全部乾淨」在輸出上一模一樣。")
    gaps = [g for g in _finiteness_gaps(trees) if g not in _FINITENESS_EXEMPT]
    assert not gaps, (
        f"這些地方把設定檔裡的數字當成量用，卻沒問過有限性：{gaps}。"
        "`1e400` 是合法 JSON、parse 出來就是 `inf`，而 `inf > 0` 為真，所以"
        "`isinstance(v, (int, float)) and v > 0` 這種寫法一路放行"
        "（`nan` 反而擋得掉——所有比較對 nan 都是假，這個不對稱正是它難發現的"
        "原因）。加一個 `_is_finite_number(v)`（用**比較**、不要呼叫 "
        "`math.isfinite()`，後者對超大 int 會溢位），真的不適用就寫進 "
        "`_FINITENESS_EXEMPT` 並說明壞值會被誤讀成什麼。")


def test_the_finiteness_scanner_can_still_see_a_violation():
    """偵測器自己的 canary。真實資料修乾淨之後上面那支永遠是綠的，牙齒在這裡。"""
    bad = ast.parse(
        "import json\n"
        "def load():\n"
        "    v = json.loads('{}').get('k')\n"
        "    if isinstance(v, (int, float)) and v > 0:\n"
        "        return float(v)\n"
        "    return 1.0\n")
    assert _finiteness_gaps([("synthetic.py", bad)]) == ["synthetic.py:load"]

    good = ast.parse(
        "import json, math\n"
        "def load():\n"
        "    v = json.loads('{}').get('k')\n"
        "    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:\n"
        "        return float(v)\n"
        "    return 1.0\n")
    assert _finiteness_gaps([("synthetic.py", good)]) == [], (
        "問過有限性的寫法被誤報了")

    ours = ast.parse(
        "import json\n"
        "def load():\n"
        "    v = json.loads('{}').get('k')\n"
        "    if _is_finite_number(v) and v > 0:\n"
        "        return float(v)\n"
        "    return 1.0\n")
    assert _finiteness_gaps([("synthetic.py", ours)]) == [], (
        "本專案自己的述詞沒有被認出來")

    formatting = ast.parse(
        "import json\n"
        "def as_text(v):\n"
        "    json.loads('{}')\n"
        "    if isinstance(v, (int, float)) and not isinstance(v, bool):\n"
        "        return str(v)\n"
        "    return ''\n")
    assert _finiteness_gaps([("synthetic.py", formatting)]) == [], (
        "只拿去 `str()` 的地方被誤抓了——`inf` 在那裡沒有任何危害")


def test_the_finiteness_module_floor_fires_when_the_enumerator_is_empty(
        monkeypatch):
    """下限的對照組：真實列舉本來就回十幾個模組，所以 `>= 8` 是量不出來的。"""
    monkeypatch.setattr(sys.modules[__name__], "_config_reading_modules",
                        lambda: [])
    with pytest.raises(AssertionError) as excinfo:
        test_no_config_number_is_admitted_without_asking_if_it_is_finite()
    assert "抽取器壞了" in str(excinfo.value), excinfo.value


def test_the_finiteness_scope_is_a_glob_not_a_list_that_matches_today(tmp_path):
    """§8.8(A3)：範圍要有自己的釘子，而釘子要分得出「算出來的」與「今天剛好對」。

    餵一個**任何手寫清單都不可能有**的名字。順便釘住負面方向：不讀 JSON 的模組
    不該進來（否則掃描範圍會膨脹成全專案，假陽性一堆）。
    """
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "brand_new_config_loader.py").write_text(
        "import json\ndef load():\n    return json.loads('{}')\n",
        encoding="utf-8")
    (root / "brand_new_root_loader.py").write_text(
        "import json\ndef load():\n    return json.load(open('x'))\n",
        encoding="utf-8")
    (pkg / "no_json_here.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "test_double.py").write_text(
        "import json\ndef load():\n    return json.loads('{}')\n",
        encoding="utf-8")

    names = {name for name, _tree in _config_reading_modules(pkg_root=pkg,
                                                             repo_root=root)}
    assert names == {"brand_new_config_loader.py", "brand_new_root_loader.py"}, (
        f"列舉回了 {sorted(names)}——不是套件那一半被縮掉、就是 repo root 那一半"
        "不見了，或者連不讀 JSON 的模組都掃了進來。")


def test_the_finiteness_exemptions_are_not_stale():
    """具名例外是 fail-open 的：函式改名之後那一筆會變成永遠不再命中的字串，
    而守門照跑、集合還在、全綠——`_OWNER_ONLY_SLASH` 的同一個失效方式。"""
    live = set(_finiteness_gaps(_config_reading_modules()))
    stale = sorted(set(_FINITENESS_EXEMPT) - live)
    assert not stale, (
        f"這幾筆豁免已經不對應任何位置（改名了、或已經修好了）：{stale}")
    for key, reason in _FINITENESS_EXEMPT.items():
        assert len(reason) > 40, f"{key} 的豁免理由太短，寫清楚壞值會被誤讀成什麼"


def test_the_exemption_reconciliation_actually_compares(monkeypatch):
    """對照組：清單乾淨時上面那支兩個方向都是空的，刪掉比較照樣綠。

    ⚠️ **這支自己踩過 §8.8(A4) 的那個坑，寫法是修正後的版本。** 第一版餵的假理由
    只有 28 個字，於是 `assert len(reason) > 40` **先**炸掉——控制測試看到
    `AssertionError`、而且訊息裡剛好也有 `nowhere.py:gone`（那句的格式是
    `f"{key} 的豁免理由太短…"`），於是它綠著，而「對帳永遠回空」那個變異存活。
    兩件事一起做才擋得住：假理由要**夠長**（讓後面那道過關），錨點要挑**只有
    對帳那一句才有**的字。
    """
    monkeypatch.setattr(
        sys.modules[__name__], "_FINITENESS_EXEMPT",
        {"nowhere.py:gone":
         "這一筆刻意指向一個不存在的位置，用來證明對帳真的會叫；理由本身寫得夠長，"
         "才不會讓長度那一道先炸掉、把控制測試餵飽而放走真正要抓的變異。"})
    with pytest.raises(AssertionError) as excinfo:
        test_the_finiteness_exemptions_are_not_stale()
    assert "不對應任何位置" in str(excinfo.value), (
        f"紅的不是對帳那一句，而是：{excinfo.value}")
    assert "nowhere.py:gone" in str(excinfo.value), excinfo.value


def test_the_scanner_would_have_caught_the_real_defect():
    """把**磁碟上真的那個檔案**改壞一行，掃描必須紅。

    合成語料證明的是偵測邏輯；這一支證明的是「偵測邏輯真的對得上磁碟上那一行」
    ——範圍、判準、`json` 的認法，中間任何一環錯掉都會讓它沉默。而且這不是假想的
    形狀：`discord_rpc.load_rpc_config` 在 2026-09-10 之前就是這樣寫的，實測
    `{"refresh_sec": 1e400}` 會讓保活永久關閉。

    改的是**記憶體裡的副本**，不是磁碟上那一份。
    """
    path = PKG_ROOT / "discord_rpc.py"
    source = path.read_text(encoding="utf-8")
    anchor = "    if _is_finite_number(rs) and rs > 0:"
    assert anchor in source, (
        f"`{anchor.strip()}` 不在 `discord_rpc.py` 裡了。寫法換了就把這裡的錨點"
        "一起換掉——別把這支測試刪掉，它是這道掃描唯一的端對端證明。")
    broken = source.replace(
        anchor,
        "    if isinstance(rs, (int, float)) and not isinstance(rs, bool) "
        "and rs > 0:")
    gaps = _finiteness_gaps([("discord_rpc.py",
                              ast.parse(broken, "discord_rpc.py"))])
    assert gaps == ["discord_rpc.py:load_rpc_config"], (
        f"把 `refresh_sec` 的檢查改回 2026-09-10 之前的寫法，掃描竟然回 {gaps}"
        "——判準或範圍已經對不上磁碟上那一行了。")
    # 反方向：現在這一份必須是乾淨的（否則上面那句可能只是永遠都在叫）。
    assert _finiteness_gaps([("discord_rpc.py",
                              ast.parse(source, "discord_rpc.py"))]) == []
# ---------------------------------------------------------------------------
# 列舉型設定值：三個 coercer 的拒絕路徑，以及「壞值不得升權」
# ---------------------------------------------------------------------------
# 分支覆蓋率（2026-09-21）指出 `_coerce_help_lang` / `_coerce_dorossi_backend` /
# `_coerce_dorossi_cc_tools` 三支的 `if low in _VALID_…` **False 那一邊從來沒有被
# 走過**——也就是「使用者打錯字」這條路一次都沒驗過。三支是同一段六行程式碼的三份
# 抄本，所以放到同一組語料上比。
#
# `dorossi_cc_tools` 那一份不只是設定，它帶著權限：`"full"` 模式會拿掉核准關卡，
# 讓後端在主機上無確認執行 shell 與讀寫檔案（`CLAUDE.md` Layer 3 明寫）。所以對
# 它還多守一條——**壞值退回去的那個值必須是權限最小的那一個**，而不只是「宣告的
# 預設值」。今天兩者剛好相同（預設是 `off`），但那是巧合等級的保證：這台機器的
# `bot_config.json` 實際設的是 `full`，只要有人覺得「預設也改成 full 比較順手」，
# 一個打錯的值就會從「失去 full 模式」變成「未經核准取得 shell」。

_ENUM_COERCERS = [
    ("help_lang", "_coerce_help_lang", "_VALID_HELP_LANGS"),
    ("dorossi_backend", "_coerce_dorossi_backend", "_VALID_DOROSSI_BACKENDS"),
    ("dorossi_cc_tools", "_coerce_dorossi_cc_tools", "_VALID_DOROSSI_CC_TOOLS"),
]


@pytest.mark.parametrize("label,func_name,valid_name", _ENUM_COERCERS,
                         ids=[row[0] for row in _ENUM_COERCERS])
def test_an_enum_coercer_rejects_what_is_not_in_its_set(
        label, func_name, valid_name):
    """三支列舉 coercer 對同一組語料要有同一種行為。

    用 `_REJECTED` sentinel 判定「被拒絕」而不是比對值——理由寫在這個檔上面那段：
    `" FULL "` 正規化成 `"full"` 是**正當的正規化**，拿值去比會把它誤報成拒絕。
    """
    coerce = getattr(bo, func_name)
    valid = sorted(getattr(bo, valid_name))
    assert valid, f"{valid_name} 是空的，這一格等於沒測"
    good = valid[0]
    # 合法值：原樣、前後空白 ＋ 大寫，都要被接受並正規化成小寫。
    assert coerce(good, bo._REJECTED) == good
    assert coerce(f"  {good.upper()}  ", bo._REJECTED) == good, (
        f"{label}：正當的正規化被當成拒絕了")
    # 壞值：一律退回 default（用 sentinel 判定）。
    for bad in ("", "   ", good + "x", "nope", None, 7, True, [], {}, 1.5):
        assert coerce(bad, bo._REJECTED) is bo._REJECTED, (
            f"{label}：{bad!r} 不在 {valid} 裡，卻被收下了")


def test_a_typo_in_the_privileged_key_can_never_escalate():
    """`dorossi_cc_tools` 打錯字時，退回去的必須是**權限最小**的那個值。

    這不是「預設值是什麼」的問題，是「壞值往哪一邊倒」的問題。`"full"` 會拿掉
    核准關卡（主機上無確認執行 shell），所以合法值裡權限最小的是 `"off"`，而
    宣告的預設值必須就是它。

    今天兩者相同，所以這支是純粹的防迴歸——但它守的是一個很容易被「順手」改掉的
    東西：這台機器實際設的是 `full`，把預設也改成 `full` 看起來只是少打一行設定。
    """
    valid = set(bo._VALID_DOROSSI_CC_TOOLS)
    assert valid == {"off", "full"}, (
        f"合法值變成 {sorted(valid)} 了——請重新判斷哪一個權限最小，"
        "再更新下面這行斷言與它的理由。")
    assert bo._DEFAULT_BOT_CONFIG["dorossi_cc_tools"] == "off", (
        "宣告的預設值不是權限最小的那個。設定檔裡一個打錯的值會退回它，"
        "於是打錯字＝未經核准取得主機 shell。")


def test_a_typo_in_the_privileged_key_really_lands_on_off(tmp_path, monkeypatch):
    """走完整條載入路徑再確認一次：檔案裡寫錯，載出來是 `off`。

    上面那支是靜態的（比對兩個常數），這一支是行為的——中間還隔著 `_take`、
    警告去重與整個 `load_bot_config`，其中任何一層把壞值原樣放行都不會被上面
    那支看到。
    """
    cfg = _bot_cfg_at(
        tmp_path, monkeypatch,
        '{"dorossi_cc_tools": "FULLL"}', "bot_config.json")
    assert cfg["dorossi_cc_tools"] == "off", (
        f"打錯的值載出來是 {cfg['dorossi_cc_tools']!r}")


def test_the_privileged_key_still_accepts_the_real_thing(tmp_path, monkeypatch):
    """近似反例：拒絕壞值不得變成「永遠回 off」。

    少了這一格，把 `_coerce_dorossi_cc_tools` 改成 `return default` 也是綠的，
    而那會讓擁有者的 `full` 設定安靜失效——一個「安全方向」的缺陷同樣是缺陷。
    """
    cfg = _bot_cfg_at(
        tmp_path, monkeypatch,
        '{"dorossi_cc_tools": "  FULL  "}', "bot_config.json")
    assert cfg["dorossi_cc_tools"] == "full", (
        f"合法（只是大小寫與空白不同）的值被丟掉了：{cfg['dorossi_cc_tools']!r}")


# ---------------------------------------------------------------------------
# `/config set` 開放的鍵 vs 載入器真的認得的鍵（2026-09-21）
# ---------------------------------------------------------------------------
#
# 上面那支對帳的是**載入器內部**的兩份（`_COERCERS` ↔ `_DEFAULT_BATCH_CONFIG`）。
# 跨到 bot 那一側還有第三份：`discord_bot._BATCH_SETTERS`，它同時決定
# `/config set` 能設什麼、以及 `/config` 顯示什麼。在這之前沒有任何東西把它跟載入器
# 比對過。
#
# 兩個方向的嚴重度差很多，所以刻意用不同強度：
#
# * **bot 讓人設、但載入器沒有這個鍵** → `/config set` 回報成功、值真的寫進
#   `batch_config.json`，而載入器從來不讀它。使用者得不到任何提示，行為也不會變。
#   這個方向 fail-open，用包含關係擋死。
# * **載入器認得、但 bot 不給設** → 那是**刻意的白名單**（`_BATCH_SETTERS` 上面那行
#   註解就寫著「Only these keys are settable」）。拿等號去比會逼人把白名單改寫成
#   as-built，等於把「刻意不開放」這個資訊刪掉——與 `CLAUDE.md` 對「Permitted third
#   channel」那份**權限**清單刻意不做 as-built 比對，是同一個道理。
#
# 所以第二個方向不拿 `_COERCERS` 去比等號，只釘住那份**名單**：多一個不可設定的鍵
# 時紅一次，逼人做一個決定（開放、或是連理由一起列進來），而不是讓它安靜長大。
#
# 2026-09-21 量測：可設定 **11**、載入器認得 **15**，只有載入器有的四個是
# `keep_system_awake`、`model_candidates`、`quota_wait_max_sec`、`quota_wait_poll_sec`。
# ⚠️ **這四個為什麼不開放，樹上沒有任何寫下來的理由**，所以這裡刻意**不替它們編**
# 一個——一個編出來的理由比沒有理由更糟，它會讓下一個人不再去問（本 repo 為「說謊的
# 理由欄」吃過虧）。這一條等人決定，連帶一個相關的落差：
# `cmd_config` 的 docstring 說它顯示「the effective batch_config.json」，但它迭代的是
# `_BATCH_SETTERS`，所以那四個鍵的值在 Discord 上**看不到**。
_LOADER_ONLY_KEYS = frozenset({
    "keep_system_awake", "model_candidates",
    "quota_wait_max_sec", "quota_wait_poll_sec",
})


def _unsettable_drift(actual: set) -> str | None:
    """不可設定的鍵有沒有漂掉？有就回一句話，沒有回 `None`。

    釘**名單**而不是數量：數量只擋得住「一進一出」以外的變化，而名單順便把「現在
    到底是哪四個」寫進測試裡。比較抽成一支的理由同本檔其他幾處——真實資料永遠剛好
    相等，所以把 `==` 放寬成任何恆真的形狀，在真樹上都看不出來。
    """
    grew = sorted(actual - _LOADER_ONLY_KEYS)
    shrank = sorted(_LOADER_ONLY_KEYS - actual)
    if not grew and not shrank:
        return None
    return (f"多出來、Discord 設不了的鍵：{grew}；"
            f"原本設不了、現在可以設或已經不存在的：{shrank}。"
            "新增一個不可設定的鍵請先確認那是刻意的，再更新這份名單，"
            "並把理由一起寫下來。")


def test_the_unsettable_drift_check_catches_both_directions():
    """兩個方向各造一次——真實資料一種都到不了（`None` 那格是 must-allow）。"""
    assert _unsettable_drift(set(_LOADER_ONLY_KEYS)) is None
    grew = _unsettable_drift(set(_LOADER_ONLY_KEYS) | {"brand_new_key"})
    assert grew and "brand_new_key" in grew, grew
    shrank = _unsettable_drift(set(_LOADER_ONLY_KEYS) - {"keep_system_awake"})
    assert shrank and "keep_system_awake" in shrank, shrank


def _settable_keys() -> set:
    import discord_bot as b

    return set(b._BATCH_SETTERS)


def _orphan_settable_keys(settable: set, honoured: set) -> list:
    """bot 設得了、但載入器根本不讀的鍵。

    **抽成一支是為了讓合成語料跑得到它。** 真實資料永遠是空的，所以
    `assert not orphans` 在真樹上對「有算」和「回傳空清單」給出一樣的答案——把整個
    計算換成 `[]` 也照樣全綠。今天已經在別的守門上量到同一件事兩次，所以這裡直接
    照著寫，不等變異測試再來補。
    """
    return sorted(settable - honoured)


def test_the_orphan_check_can_actually_see_an_orphan():
    """合成對照組：比對真的在算，而且不會把正常情況誤報成問題。"""
    assert _orphan_settable_keys({"a", "b"}, {"a"}) == ["b"]
    assert _orphan_settable_keys({"a"}, {"a", "b"}) == []      # must-allow
    assert _orphan_settable_keys(set(), {"a"}) == []


def test_every_settable_key_is_one_the_loader_actually_reads():
    """bot 開放設定的每一個鍵，載入器都必須真的讀它。

    對不上的話 `/config set` 會回報成功、值寫進檔案，而載入器根本不看——使用者以為
    改了設定，行為卻完全沒變，而且沒有任何訊息說出這件事。
    """
    settable = _settable_keys()
    assert len(settable) >= 8, (
        f"只抽到 {len(settable)} 個可設定的鍵——`_BATCH_SETTERS` 的形狀變了？"
        "下面那行等於沒在檢查。")
    orphans = _orphan_settable_keys(settable, set(bc._COERCERS))
    assert not orphans, (
        f"這些鍵 `/config set` 設得了，但載入器沒有對應的 coercer：{orphans}。"
        "指令會回報成功、值也真的寫進 `batch_config.json`，但載入器從來不讀它，"
        "所以行為一點都不會變——而使用者不會收到任何提示。")


def test_the_set_of_loader_only_keys_has_not_drifted():
    """載入器認得、但 Discord 設不了的鍵，名單不得悄悄漂掉。

    **不拿 `_COERCERS` 去比等號**：不開放是刻意的（理由見上面那段）。但「又多了一個
    設不了的鍵」應該是有人決定過的結果，不是沒人注意到的副作用，所以釘住 `_LOADER_ONLY_KEYS`
    這份名單——釘名單而不是數量，順便把「現在到底是哪四個」寫進測試裡。
    名單變了就來這裡改，連同理由一起寫下來。
    """
    problem = _unsettable_drift(set(bc._COERCERS) - _settable_keys())
    assert problem is None, problem


# --- 衍生守門：`_BATCH_SETTERS` 每個鍵 × 一池敵意輸入（2026-09-21） ---------------
#
# 上面那支 `test_nothing_the_bot_accepts_silently_reverts` 用的是手挑的正常樣本，
# 所以 2026-09-21 差分探針找到的兩個分歧（超大整數、區間值吃掉負號）它都看不到；
# 逐案測試補上了那兩個，但下一個分歧一樣會落在「沒人想到的輸入」上。這一支把判準
# 寫成性質，對 `_BATCH_SETTERS` 的**每一個**鍵（包括以後新增的）跑同一池輸入：
#
# 一、**載入器認得。** bot 收下的值經過真的 `save_batch_config` 寫檔再讀回來，必須
#     原樣得到——退回預設就是「回報成功但沒有生效」。寫不出去（非法 JSON）也算。
# 二、**不吃掉負號。** 文字裡落在非零數字上的負號，bot 一律不得收下。這一條只有
#     文字那一側看得到：`-3,5` 被存成 `[3, 5]` 時，載入器拿到的是一個完全合法的值，
#     第一條對帳天生看不見。判定刻意**不**呼叫 `_parse_cfg_pair`（見 `_typed_a_negative`）。
# 三、**顯示得出來的就打得回去。** `/config` 用 `_fmt_cfg_value` 顯示目前的值，使用者
#     最自然的操作就是把它照抄回 `/config set`。所以讀回來的值經過顯示、再經過解析，
#     必須得到同一個值。每個鍵的**預設值**也放進輸入池——那是 `/config` 最常顯示的
#     東西，而 2026-09-21 之前區間的預設值 `20–30`（en dash）照抄回去是被擋掉的。

_SETTER_POOL_COMMON = (
    # 數字：邊界、非有限值、Python 認得但人不會打的寫法
    "0", "1", "-1", "-0", "2.5", "1e3", "1e-3", "1e-320", "1_000", "１２", "٣", "0x10",
    "9" * 308, "9" * 309, "1" * 400, "1e308", "1e309", "inf", "-inf", "nan",
    "0.0", "1.0", "1.5", " 5 ", "+5", "5.", ".5", "1,000", "−3",
    # 布林
    "true", "false", "TRUE", "yes", "on", "y", "n", "off",
    # 區間：合法寫法、負號的各種位置、指數、分隔符的全形與 en dash
    "20,30", "3-5", "3 - 5", "3- 5", "3 5", "0,0", "1.5,2.5", "3,,5", ",3,5",
    "-3,5", "0,-3", "3--5", "3 -5", "-3 5", "--3,5", "-0,0", "-0.5,1", "3---5",
    "1e-3,5", "1e-3-5", "1e-3 1e-2", "1e+3,2e3", "20，30", "3–5", "3 – 5",
    "–3,5", "3,5–", "inf,inf", "1e400,1e400", "nan,nan", "−3,5",
)

# 兩個方向的下限：2026-09-21 量到收下 123、擋下 636（十一個鍵合計），往下取整留餘裕。
# 收下那一側掉下去，代表某個鍵的 parser 變嚴到整池都擋——性質雖然還成立，但已經
# 沒有東西在檢查它；擋下那一側掉下去，代表輸入池被人刪短了。
_SETTER_ACCEPTED_FLOOR = 110
_SETTER_REJECTED_FLOOR = 570

_RANGE_DASHES = "-–"
_NUMBER_AT = re.compile(r"[0-9.]+(?:[eE][+-]?[0-9]+)?")


def _typed_a_negative(text: str) -> bool:
    """文字裡有沒有一個負號落在**非零**的數字上——與 `_parse_cfg_pair` 無關的獨立判定。

    `-`／`–` 只有在兩側（略過空白）都緊鄰數字本體時才算區間分隔（`3-5`、`3 – 5`）；
    緊跟在 e/E 後面的是指數（`1e-3`）；其餘位置都是正負號。後面接的數字是零
    （`-0`）不算負數，後面接的東西不是數字（`--3`、`-inf`）則一律當成負數。
    """
    for index, char in enumerate(text):
        if char not in _RANGE_DASHES:
            continue
        if char == "-" and index and text[index - 1] in "eE":
            continue
        before = text[:index].rstrip()
        after = text[index + 1:].lstrip()
        if before[-1:] and before[-1] in "0123456789." and after[:1] \
                and after[0] in "0123456789.":
            continue
        number = _NUMBER_AT.match(after)
        if number and float(number.group()) == 0:
            continue
        return True
    return False


def _same_setting(left, right) -> bool:
    """布林比身分、序列逐項比、數字比值（`int == float` 在 Python 是精確比較）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        return (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))
                and len(left) == len(right)
                and all(a == b for a, b in zip(left, right)))
    return left == right


def _setter_parity_problems(setters: dict, store, fmt, pool: dict):
    """回傳 `({key: {"accepted": n, "rejected": m}}, [(種類, key, 輸入, 說明)])`。

    `store(key, value)` 寫進去再讀回來、回傳載入器給的值；`fmt(value)` 是顯示。
    種類：`unwritable`／`reverted`／`sign`／`display`。
    """
    counts: dict = {}
    problems: list = []
    for key, parser in setters.items():
        tally = counts.setdefault(key, {"accepted": 0, "rejected": 0})
        for text in pool[key]:
            value, error = parser(text)
            if error:
                tally["rejected"] += 1
                continue
            tally["accepted"] += 1
            if _typed_a_negative(text):
                problems.append(("sign", key, text, f"文字帶負號，卻收下 {value!r}"))
            try:
                loaded = store(key, value)
            except (ValueError, TypeError, OverflowError) as exc:
                problems.append(("unwritable", key, text, f"收下 {value!r} 卻寫不出去：{exc!r}"))
                continue
            if not _same_setting(loaded, value):
                problems.append(("reverted", key, text,
                                 f"收下 {value!r}，載入器卻給 {loaded!r}"))
                continue
            shown = fmt(loaded)
            again, error = parser(shown)
            if error or not _same_setting(again, loaded):
                problems.append(("display", key, text,
                                 f"顯示成 {shown!r}，照抄回去得到 {again!r}／{error!r}"))
    return counts, problems


def test_the_setter_parity_check_can_actually_see_every_disagreement():
    """合成對照：四種分歧各放一個會犯它的假 parser，外加一個乾淨的，確認判定真的看得見。

    真實資料在修好之後是乾淨的，於是「回報分歧」那幾行在真實資料上一次都不會執行——
    刪掉任何一個 `problems.append` 都還是綠的。這支讓每一條都有東西可以抓。
    """
    def reverts(text):          # 收下任何整數，但「載入器」只認 ≤ 100
        try:
            return int(text), ""
        except ValueError:
            return None, "no"

    def eats_sign(text):        # 老寫法：`-` 一律當分隔
        parts = [p for p in re.split(r"[,\s\-–]+", text.strip()) if p]
        try:
            return [float(p) for p in parts], "" if len(parts) == 2 else "no"
        except ValueError:
            return None, "no"

    def comma_only(text):       # 只認逗號，但顯示用 en dash
        parts = text.split(",")
        try:
            return ([float(p) for p in parts], "") if len(parts) == 2 else (None, "no")
        except ValueError:
            return None, "no"

    def unwritable(text):       # 收下 inf
        try:
            return float(text), ""
        except ValueError:
            return None, "no"

    def clean(text):
        return (int(text), "") if text.isascii() and text.isdigit() else (None, "no")

    def store(key, value):
        if key == "reverts" and value > 100:
            return 5
        if key == "pair_reverts":       # 區間被退回預設：逐項比較才看得出來
            return (20.0, 30.0)
        if key == "unwritable":
            json.dumps(value, allow_nan=False)
        return value

    def fmt(value):
        if isinstance(value, list):
            return f"{value[0]:g}–{value[1]:g}"
        return str(value)

    setters = {"reverts": reverts, "eats_sign": eats_sign, "comma_only": comma_only,
               "unwritable": unwritable, "clean": clean, "pair_reverts": eats_sign}
    pool = {"reverts": ["7", "500"], "eats_sign": ["3,5", "-3,5"],
            "comma_only": ["3,5"], "unwritable": ["2", "inf"], "clean": ["7", "x"],
            "pair_reverts": ["3,5"]}
    counts, problems = _setter_parity_problems(setters, store, fmt, pool)
    kinds = sorted((kind, key, text) for kind, key, text, _ in problems)
    assert kinds == [("display", "comma_only", "3,5"),
                     ("reverted", "pair_reverts", "3,5"),
                     ("reverted", "reverts", "500"),
                     ("sign", "eats_sign", "-3,5"),
                     ("unwritable", "unwritable", "inf")], problems
    assert counts["clean"] == {"accepted": 1, "rejected": 1}
    assert counts["reverts"] == {"accepted": 2, "rejected": 0}


@pytest.mark.parametrize("text, negative", [
    ("-3,5", True), ("0,-3", True), ("3--5", True), ("--3,5", True), ("-inf", True),
    ("–3,5", True), ("3,5–", True), ("3 -5", False), ("3-5", False), ("3 – 5", False),
    ("1e-3,5", False), ("1e-3-5", False), ("-0,0", False), ("-0", False), ("20,30", False),
])
def test_the_negative_sign_oracle_pins_both_directions(text, negative):
    """這個判定是守門的另一半，它自己錯了守門就跟著錯；逐案釘住兩個方向。

    `3 -5` 判成「不是負數」是刻意的寬鬆：這個判定只用來抓 bot **收下**的文字，
    bot 比它嚴（把 `3 -5` 讀成 3 與 −5 而擋下）是允許的方向。
    """
    assert _typed_a_negative(text) is negative


def test_whatever_the_bot_accepts_is_kept_signed_and_displayable(tmp_path, monkeypatch):
    """`_BATCH_SETTERS` 每個鍵 × `_SETTER_POOL_COMMON` ＋ 該鍵預設值的顯示字串。"""
    import discord_bot as b

    path = _write_batch(tmp_path, monkeypatch, "{}")

    def store(key, value):
        path.write_text("{}", encoding="utf-8")
        return bc.save_batch_config({key: value})[key]

    pool = {key: list(_SETTER_POOL_COMMON)
            + [b._fmt_cfg_value(bc._DEFAULT_BATCH_CONFIG[key])]
            for key in b._BATCH_SETTERS}
    counts, problems = _setter_parity_problems(
        b._BATCH_SETTERS, store, b._fmt_cfg_value, pool)
    assert not problems, "\n".join(
        f"[{kind}] {key}={text!r}：{detail}" for kind, key, text, detail in problems[:20])
    accepted = sum(c["accepted"] for c in counts.values())
    rejected = sum(c["rejected"] for c in counts.values())
    assert accepted >= _SETTER_ACCEPTED_FLOOR, counts
    assert rejected >= _SETTER_REJECTED_FLOOR, counts
    for key, tally in counts.items():
        assert tally["accepted"] and tally["rejected"], (
            f"{key} 在整池輸入裡{'一個都沒收下' if not tally['accepted'] else '一個都沒擋'}"
            f"——這個鍵的性質其實沒有被檢查到：{tally}")


# --- `_bot_config` 兩支從來沒走過拒絕那一邊的 coercer（2026-09-21 分支覆蓋率盤點） ---

@pytest.mark.parametrize("flag", [True, False])
def test_an_id_list_drops_booleans_both_ways(flag):
    """`bool` 是 `int` 的子類：沒有那一行的話 `true` 會變成使用者 id 1、`false` 變成 0。
    兩個布林都要測——只測 `False` 的話，`x >= 1` 這類下限會替它擋掉，看不出那行在不在。"""
    assert bo._coerce_int_list([flag, 123, " 456 ", -1, "x", 1.5]) == [123, 456]


def test_an_id_list_that_is_not_a_list_is_empty():
    assert bo._coerce_int_list("123") == []
    assert bo._coerce_int_list(None) == []


@pytest.mark.parametrize("bad", [None, 3, "", "   ", ["03:00"]])
def test_a_report_time_that_is_not_text_falls_back(bad):
    assert bo._coerce_time_str(bad, "04:30") == "04:30"


def test_a_report_time_is_stripped_not_rewritten():
    assert bo._coerce_time_str("  23:15 ", "04:30") == "23:15"
