"""`_run_progress` 純函式測試（無 Selenium、不碰真正的 repo checkpoint）。

驗證硬殺 / 崩潰中斷後能正確續跑的關鍵不變式：
1. 原子寫入：`write_progress` 產生合法 JSON，且 `saved` 預設為 0。
2. `update_saved`：只更新 `saved`，pair 欄位 / folder / target 全保留。
3. 容錯：被截斷成半截的 JSON，`read_progress` 必須回 None（不丟例外），
   且 `update_saved` 在 checkpoint 缺失 / 損毀時 no-op。
4. `matches` 維持四欄全等語意；`diagnose_mismatch` 指出歧異欄位。

每個測試把 `_run_progress` 的檔案路徑重導到 tmp，跑完還原，絕不汙染
repo 根目錄的 `webrunner_progress.json`。

可直接 `py -3 test/test_run_progress.py`（自帶 runner），也可 pytest。
"""

import ast as _ast
import pathlib as _pathlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import _run_progress as rp  # noqa: E402


class _TempCheckpoint:
    """把 PROGRESS_FILE / _PROGRESS_TMP 重導到一個全新 tmp 目錄；離開時還原。"""

    def __enter__(self):
        self._saved = (rp.PROGRESS_FILE, rp._PROGRESS_TMP)
        self._dir = tempfile.mkdtemp(prefix="rp_test_")
        rp.PROGRESS_FILE = Path(self._dir) / "webrunner_progress.json"
        rp._PROGRESS_TMP = rp.PROGRESS_FILE.with_suffix(".json.tmp")
        return self

    def __exit__(self, *exc):
        rp.PROGRESS_FILE, rp._PROGRESS_TMP = self._saved
        # 清掉 tmp 目錄（best-effort）。
        for p in Path(self._dir).iterdir():
            try:
                p.unlink()
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass


def test_write_is_valid_json_and_seeds_saved_zero():
    print("test_write_is_valid_json_and_seeds_saved_zero")
    with _TempCheckpoint():
        rp.write_progress("P", "c1", "c2", "u", "Alice", 240)
        # 直接讀原始 bytes 確認是合法、完整的 JSON（非半截）。
        raw = rp.PROGRESS_FILE.read_text(encoding="utf-8")
        obj = json.loads(raw)
        assert obj["prompt"] == "P"
        assert obj["char1"] == "c1"
        assert obj["char2"] == "c2"
        assert obj["undesired"] == "u"
        assert obj["folder"] == "Alice"
        assert obj["target"] == 240
        assert obj["saved"] == 0, "saved 必須在角色起始預設為 0"
        # 原子寫入後不該有殘留 .tmp。
        assert not rp._PROGRESS_TMP.exists(), "os.replace 後 .tmp 應已被移除"
    print("  PASS\n")


def test_update_saved_preserves_fields_and_bumps_count():
    print("test_update_saved_preserves_fields_and_bumps_count")
    with _TempCheckpoint():
        rp.write_progress("P", "c1", "c2", "u", "Alice", 240)
        rp.update_saved(137)
        prog = rp.read_progress()
        assert prog["saved"] == 137, "saved 應被更新"
        # pair 欄位 / folder / target 全保留。
        for k, v in (("prompt", "P"), ("char1", "c1"), ("char2", "c2"),
                     ("undesired", "u"), ("folder", "Alice"), ("target", 240)):
            assert prog[k] == v, f"{k} 不應被 update_saved 動到"
        # 再 bump 一次（模擬下一張圖）。
        rp.update_saved(138)
        assert rp.read_progress()["saved"] == 138
    print("  PASS\n")


def test_read_survives_truncated_file():
    print("test_read_survives_truncated_file")
    with _TempCheckpoint():
        # 模擬硬殺在寫到一半：半截 JSON。
        rp.PROGRESS_FILE.write_text('{"prompt": "P", "char1"', encoding="utf-8")
        assert rp.read_progress() is None, "半截 JSON 必須回 None，不可丟例外"
        # 完全空檔也要安全。
        rp.PROGRESS_FILE.write_text("", encoding="utf-8")
        assert rp.read_progress() is None
        # 缺檔。
        rp.PROGRESS_FILE.unlink()
        assert rp.read_progress() is None
    print("  PASS\n")


def test_update_saved_noop_when_corrupt_or_missing():
    print("test_update_saved_noop_when_corrupt_or_missing")
    with _TempCheckpoint():
        # 缺檔：update_saved 不該建立任何東西、不該丟例外。
        rp.update_saved(5)
        assert rp.read_progress() is None
        assert not rp.PROGRESS_FILE.exists()
        # 損毀檔：update_saved no-op，原始（壞）內容不被「修好」成有效 JSON。
        rp.PROGRESS_FILE.write_text("{bad", encoding="utf-8")
        rp.update_saved(9)
        assert rp.read_progress() is None
    print("  PASS\n")


def test_matches_is_four_field_exact():
    print("test_matches_is_four_field_exact")
    prog = {"prompt": "P", "char1": "c1", "char2": "c2", "undesired": "u",
            "folder": "Alice", "target": 240, "saved": 10}
    assert rp.matches(prog, "P", "c1", "c2", "u")
    assert not rp.matches(prog, "P", "c1", "c2", "DIFFERENT")
    assert not rp.matches(None, "P", "c1", "c2", "u")
    print("  PASS\n")


def test_diagnose_mismatch_reports_diverging_fields():
    print("test_diagnose_mismatch_reports_diverging_fields")
    prog = {"prompt": "P", "char1": "c1", "char2": "c2", "undesired": "u"}
    # 全等 → 無歧異。
    assert rp.diagnose_mismatch(prog, "P", "c1", "c2", "u") == []
    # 兩欄歧異 → 兩行，且點名欄位。
    lines = rp.diagnose_mismatch(prog, "P2", "c1", "c2", "u2")
    assert len(lines) == 2
    assert any(l.startswith("prompt:") for l in lines)
    assert any(l.startswith("undesired:") for l in lines)
    # None checkpoint → 空 list（不是 diagnostic 目標）。
    assert rp.diagnose_mismatch(None, "P", "c1", "c2", "u") == []
    print("  PASS\n")


def test_mismatch_fields_names_only():
    """`mismatch_fields` 只回欄位**名稱**——它的輸出會被寫進 events.ndjson，
    欄位內容（提示詞全文）不可以跟著跑出去。順序照 `IDENTITY_FIELDS`。"""
    print("test_mismatch_fields_names_only")
    prog = {"prompt": "P", "char1": "c1", "char2": "c2", "undesired": "u"}
    assert rp.mismatch_fields(prog, "P", "c1", "c2", "u") == []
    assert rp.mismatch_fields(prog, "P2", "c1", "c2", "u") == ["prompt"]
    # 多欄歧異照 IDENTITY_FIELDS 的順序回，不是字典序。
    assert rp.mismatch_fields(prog, "P2", "c1", "X", "u2") == [
        "prompt", "char2", "undesired"]
    # 回傳值裡不得出現任何欄位內容。
    names = rp.mismatch_fields(prog, "P2", "c1", "c2", "u")
    assert all(n in rp.IDENTITY_FIELDS for n in names)
    # 沒有 checkpoint ≠ 欄位不一致 → 空 list；`matches` 得自己擋掉這種情況。
    assert rp.mismatch_fields(None, "P", "c1", "c2", "u") == []
    assert not rp.matches(None, "P", "c1", "c2", "u")
    # 缺欄位的舊檢查點：缺的那欄算歧異，不 raise。
    assert rp.mismatch_fields({"prompt": "P"}, "P", "c1", "c2", "u") == [
        "char1", "char2", "undesired"]
    print("  PASS\n")


def test_resume_folder_rejects_every_corrupt_folder_value():
    """`matches()` 只看四個身分欄位，`folder` 它根本不碰——所以一份身分欄位
    完全相符、`folder` 卻壞掉的檢查點會讓 `matches()` 回 True。呼叫端接著拿
    那個值去接路徑，實測六種壞法：缺欄位 → KeyError；None／int／list →
    TypeError（三種都會把整批作業炸掉）；`../` 與絕對路徑 → 路徑跑到
    `output/` 外面，而且沒有人會發現。`resume_folder` 就是為了把這六種一次
    擋掉，全部退回 None＝這一輪不接續。"""
    print("test_resume_folder_rejects_every_corrupt_folder_value")
    root = Path("/tmp/out") if os.name != "nt" else Path("D:/out")
    base = {"prompt": "P", "char1": "c1", "char2": "c2", "undesired": "u"}
    bs = chr(92)
    bad_values = [
        None,                       # 值是 None
        5,                          # 值是數字
        ["x"],                      # 值是 list
        {"x": 1},                   # 值是 dict
        "",                         # 空字串
        "   ",                      # 只有空白
        ".",                        # 目前目錄
        "..",                       # 上一層
        "../../Windows/Temp/pwned",  # 相對路徑逃脫
        "C:/Windows/Temp/pwned",    # 絕對路徑（磁碟機代號）
        "/etc/pwned",               # 絕對路徑（POSIX）
        "sub/dir",                  # 多層
        bs + "x",                   # 反斜線——POSIX 上 Path().name 看不出來
        "a" + bs + bs + "b",
        "C:x",                      # 磁碟機相對路徑
    ]
    for value in bad_values:
        prog = dict(base, folder=value)
        # 前提：這些壞掉的檢查點在 `matches()` 眼中全都是「同一個配對」。
        assert rp.matches(prog, "P", "c1", "c2", "u"), value
        assert rp.resume_folder(prog, root) is None, value
    # 欄位整個不存在也一樣（原本是 KeyError）。
    assert rp.matches(dict(base), "P", "c1", "c2", "u")
    assert rp.resume_folder(dict(base), root) is None
    # 沒有檢查點 / 空 dict → None，不 raise。
    assert rp.resume_folder(None, root) is None
    assert rp.resume_folder({}, root) is None
    print("  PASS\n")


def test_resume_folder_accepts_the_shapes_allocate_output_dir_produces():
    """反向：正常值必須原封不動接在 `output_root` 底下，否則這道防線會把
    真正該接續的角色也擋掉（那比崩潰更難發現——只會多出一個編號資料夾）。
    `allocate_output_dir` 產得出來的形狀就是「單一層資料夾名」，角色名裡的
    空白、括號、底線、數字、非 ASCII 都要放行。"""
    print("test_resume_folder_accepts_the_shapes_allocate_output_dir_produces")
    root = Path("/tmp/out") if os.name != "nt" else Path("D:/out")
    base = {"prompt": "P", "char1": "c1", "char2": "c2", "undesired": "u"}
    for good in ["Alice", "surtr (arknights)", "surtr (arknights)_2",
                 "mi fu (arknights)_10", "角色名", "a.b.c", "1girl"]:
        got = rp.resume_folder(dict(base, folder=good), root)
        assert got == root / good, (good, got)
    # 頭尾空白會被吃掉再接（使用者手動編輯檢查點時很常見）。
    assert rp.resume_folder(dict(base, folder="  Alice  "), root) == root / "Alice"
    print("  PASS\n")


# 這條路自己餵得出來的 fixture。`monkeypatch` / `capsys` 不在裡面是刻意的：
# 手寫的替身**看起來**會動，但 `monkeypatch` 真正的價值在測試結束後把狀態還原，
# 一個還原得不完整的替身會讓後面的測試在被污染的模組狀態下跑，然後給出一個看不
# 出來是假的綠燈。餵不出來就照實少跑、把差額印出來，那是本專案既有的慣例
# （見 `test_supervisor.main`）。
_SELF_RUNNABLE_FIXTURES = {"tmp_path"}


def _run_all():
    """自帶 runner：掃 `globals()`，餵得出 fixture 的就跑，餵不出的照實報差額。

    **2026-09-10 修掉兩個缺陷。**

    一、原本是 `for t in tests: t()`，而這個檔案裡有 19 支測試收 pytest 的
    fixture，所以它跑到第一支就 `TypeError` 當掉——`py -3` 這條路等於是壞的，
    而且因為沒有人會在 pytest 之外跑它，壞了很久都沒有人發現。

    二、原本那段「另有 N 支 standalone 跑不到」是**死碼**。它是從逐支具名註冊的
    runner（`test_bot_helpers` / `test_supervisor`）抄過來的：那邊 `len(groups)`
    真的小於檔案宣告數，差額有意義。但這支是掃 `globals()` 的，`len(tests)` 與
    `declared` **恆等**，差額恆為 0，那句話一次都不會印。而
    `test_self_runners.test_the_runner_knows_how_many_tests_the_file_declares`
    刻意只檢查「有沒有從原始碼算出宣告數」（不比對函式名或訊息文字，免得變成在
    守一種寫法），於是這段恆為死碼的儀式就把守門滿足了。

    現在 `ast.parse` 的結果拿去守**真的會發生**的那件事——main 區塊前面還有測試
    沒 bind——而差額算的是「因為餵不出 fixture 而沒跑的」，那個數字是真的。
    """
    import inspect as _inspect
    import tempfile as _tempfile

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    declared = sum(
        1 for _n in _ast.parse(
            _pathlib.Path(__file__).read_text(encoding="utf-8")).body
        if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and _n.name.startswith("test_"))
    if len(tests) != declared:
        raise SystemExit(
            f"self-runner 只看得到 {len(tests)} 支測試，檔案裡卻定義了 {declared} "
            "支。多半是有人把 `if __name__ == \"__main__\"` 之後又加了測試——那些"
            "在呼叫 `_run_all()` 的當下還沒 bind，會被安靜地略過。把 main 區塊移"
            "到檔尾即可。")

    ran = 0
    skipped = []
    for t in tests:
        params = list(_inspect.signature(t).parameters)
        if set(params) - _SELF_RUNNABLE_FIXTURES:
            skipped.append(t.__name__)
            continue
        t(*[Path(_tempfile.mkdtemp(prefix="selfrun_")) for _ in params])
        ran += 1

    note = ""
    if skipped:
        note = (f"（另有 {len(skipped)} 支需要這條路餵不出來的 pytest fixture，"
                "standalone 跑不到；完整結果請跑 pytest）")
    print(f"ALL {ran} TEST GROUPS PASSED{note}")


# ===========================================================================
# `folder_image_stats` —— 續跑時「下一張圖從幾號開始」
#
# 這支決定的是**檔名**，而檔名撞號的代價是把上一輪的成果覆寫掉——那是資料遺失，
# 而且沒有任何錯誤訊息。`next_index` 刻意取「已存在的最大索引 ＋ 1」而不是
# 「張數 ＋ 1」：上一輪失敗的圖會在編號裡留下空洞，用張數算就會撞到後面那些。
#
# 2026-09-06 量覆蓋率時發現它一行都沒被跑過。
# ===========================================================================

def _png(folder: Path, index: int, stamp: str = "20260906_120000") -> None:
    (folder / f"char_{index:04d}_{stamp}.png").write_bytes(b"x")


def test_a_missing_folder_starts_from_one(tmp_path):
    assert rp.folder_image_stats(tmp_path / "nope") == (0, 1)


def test_an_empty_folder_starts_from_one(tmp_path):
    assert rp.folder_image_stats(tmp_path) == (0, 1)


def test_the_next_index_follows_the_highest_existing_one(tmp_path):
    for i in (1, 2, 3):
        _png(tmp_path, i)
    assert rp.folder_image_stats(tmp_path) == (3, 4)


def test_a_gap_does_not_pull_the_next_index_back(tmp_path):
    """**這是這支存在的理由。**

    上一輪有兩張失敗，於是資料夾裡是 1、2、5——張數是 3，但下一張必須是 6。
    用「張數 ＋ 1」會得到 4，寫下去就把 5 之後的編號空間弄亂，接著撞號覆寫。
    """
    for i in (1, 2, 5):
        _png(tmp_path, i)
    assert rp.folder_image_stats(tmp_path) == (3, 6)


def test_files_that_do_not_match_the_pattern_are_counted_but_not_indexed(
        tmp_path):
    """人手放進去的圖、或別的工具產的圖，不該被拿來推算編號。"""
    _png(tmp_path, 7)
    (tmp_path / "screenshot.png").write_bytes(b"x")
    count, nxt = rp.folder_image_stats(tmp_path)
    assert count == 2
    assert nxt == 8, "非本專案命名的檔案影響了編號"


def test_only_png_files_count(tmp_path):
    _png(tmp_path, 1)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "thumb.jpg").write_bytes(b"x")
    assert rp.folder_image_stats(tmp_path) == (1, 2)


def test_the_suffix_match_is_case_insensitive(tmp_path):
    (tmp_path / "char_0003_20260906_120000.PNG").write_bytes(b"x")
    assert rp.folder_image_stats(tmp_path) == (1, 4)


def test_an_uppercase_suffix_cannot_cause_a_filename_collision(tmp_path):
    r"""**這一支抓到的是實際的缺陷。**

    檔案過濾原本是不分大小寫的（`p.suffix.lower() == ".png"`），但抽編號的
    `_INDEX_RE` 結尾寫死 `\.png$`、分大小寫。於是這個資料夾（`_0001_.png` ＋
    `_0009_.PNG`）張數是 2、`max_idx` 只看得到 1 —— 但真正致命的是連 1 都看不到
    的情形：全部大寫時 `max_idx` 是 0，`next_index` 退回「張數 ＋ 1」，那個值
    小於已存在的最大編號，下一張圖直接**覆寫掉上一輪的成果**，而且沒有任何錯誤。
    """
    _png(tmp_path, 1)
    (tmp_path / "char_0009_20260906_120000.PNG").write_bytes(b"x")
    count, nxt = rp.folder_image_stats(tmp_path)
    assert count == 2
    assert nxt == 10, f"下一張要從 10 開始，實際 {nxt} —— 會撞到已存在的編號"


def test_a_sub_folder_is_not_counted_as_an_image(tmp_path):
    (tmp_path / "sub.png").mkdir()
    assert rp.folder_image_stats(tmp_path) == (0, 1)


def test_a_file_in_place_of_the_folder_is_not_fatal(tmp_path):
    """`iterdir()` 對檔案丟 `OSError`——續跑路徑上不能拋。"""
    path = tmp_path / "notafolder"
    path.write_text("x", encoding="utf-8")
    assert rp.folder_image_stats(path) == (0, 1)


def test_only_files_with_no_parsable_index_fall_back_to_the_count(tmp_path):
    """一張都解不出編號時退回「張數 ＋ 1」——保守，至少不會撞到已存在的編號。"""
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(b"x")
    assert rp.folder_image_stats(tmp_path) == (2, 3)


# ===========================================================================
# `clear_progress` / `atomic_write_text` —— 兩支「絕不往外拋」的
# ===========================================================================

def test_clearing_a_missing_checkpoint_is_silent(tmp_path, monkeypatch, capsys):
    """已經沒有檢查點是正常結束的常態，不該吵。"""
    monkeypatch.setattr(rp, "PROGRESS_FILE", tmp_path / "nope.json")
    rp.clear_progress()
    assert capsys.readouterr().err == ""


def test_clearing_removes_the_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "run_progress.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rp, "PROGRESS_FILE", path)
    rp.clear_progress()
    assert not path.exists()


def test_clearing_a_locked_checkpoint_says_why(tmp_path, monkeypatch, capsys):
    """刪不掉只能記一筆——這支跑在批次收尾，拋出去會把 rc 弄成非 0，
    supervisor 就會把一次乾淨收工當成崩潰、再重生一輪。"""
    monkeypatch.setattr(rp, "PROGRESS_FILE", tmp_path)   # 目錄 → unlink 丟 OSError
    rp.clear_progress()
    assert "clear failed" in capsys.readouterr().err


def test_an_atomic_write_lands_the_whole_text(tmp_path):
    target = tmp_path / "x.json"
    assert rp.atomic_write_text(target, "hello") is True
    assert target.read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "x.json.tmp").exists()


def test_an_atomic_write_replaces_the_previous_content(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("old and much longer", encoding="utf-8")
    assert rp.atomic_write_text(target, "new") is True
    assert target.read_text(encoding="utf-8") == "new"


def test_a_failed_atomic_write_reports_false_and_leaves_no_temp(
        tmp_path, monkeypatch, capsys):
    """`os.replace` **只有成功時**才會把 temp 搬走。

    失敗時不善後的話，repo root 會多一個看起來像真資料的 `.tmp`——而且回 True
    的話呼叫端會以為寫成功了。這兩件事要一起成立。
    """
    monkeypatch.setattr(
        rp.os, "replace",
        lambda _a, _b: (_ for _ in ()).throw(OSError("replace failed")))
    target = tmp_path / "x.json"
    assert rp.atomic_write_text(target, "hello") is False
    assert sorted(p.name for p in tmp_path.glob("*.tmp")) == []
    assert "failed" in capsys.readouterr().err


def test_a_failed_atomic_write_leaves_the_previous_content_intact(
        tmp_path, monkeypatch):
    """寫失敗的代價只能是「這次沒寫成」，不能是「原本的內容沒了」。

    這正是 temp ＋ `os.replace` 相對於 truncate-then-write 的全部價值。
    """
    monkeypatch.setattr(
        rp.os, "replace",
        lambda _a, _b: (_ for _ in ()).throw(OSError("nope")))
    target = tmp_path / "x.json"
    target.write_text("original", encoding="utf-8")
    rp.atomic_write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"


def test_the_temp_sits_next_to_the_target(tmp_path, monkeypatch):
    """同目錄才保證同一個檔案系統，`os.replace` 才是原子的。

    改成 `%TEMP%` 或別的目錄，跨磁碟時 `os.replace` 會退化成「複製 ＋ 刪除」，
    原子性就沒了——而失敗的樣子跟成功一模一樣。
    """
    seen = []
    real = rp.os.replace
    monkeypatch.setattr(rp.os, "replace",
                        lambda a, b: (seen.append(Path(a)), real(a, b))[1])
    target = tmp_path / "sub" / "x.json"
    target.parent.mkdir()
    rp.atomic_write_text(target, "hi")
    assert seen and seen[0].parent == target.parent, seen


if __name__ == "__main__":
    _run_all()
