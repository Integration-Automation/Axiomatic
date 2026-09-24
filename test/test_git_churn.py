"""`/sys churn`（今日 git 活動彙總）的純函式測試。

彙總邏輯（`_parse_repo_churn` / `_build_churn_report` / `_render_churn_report`）
刻意設計成**不碰機器**：只吃字串，所以這裡用合成的 `git log` 輸出就能把「今天的
總計」「各 repo 明細」「只列有活動的 repo」「壞資料／讀不到的 repo 容錯」「空結果」
全部釘住，不必真的去跑 git。掃描那一半（`_scan_workspace_churn`）碰磁碟，用注入的
runner ＋ tmp 目錄測它的略過與 degrade 行為。

    py -3 -m pytest test/test_git_churn.py
"""
from __future__ import annotations

from pathlib import Path

import discord_bot as b


# git log --format=%x1f%an --numstat 的每個 commit：一行 \x1f<作者>，後面接 numstat。
def _commit(author: str, *numstat: tuple[str, str, str]) -> str:
    lines = ["\x1f" + author]
    for added, deleted, path in numstat:
        lines.append(f"{added}\t{deleted}\t{path}")
    return "\n".join(lines)


def _repo_output(*commits: str) -> str:
    return "\n".join(commits) + "\n"


# --------------------------------------------------------------------------
# _parse_repo_churn
# --------------------------------------------------------------------------
def test_parse_sums_commits_lines_and_authors():
    out = _repo_output(
        _commit("Alice", ("10", "2", "src/x.py"), ("5", "0", "README.md")),
        _commit("Bob", ("3", "1", "src/y.py")),
    )
    commits, added, deleted, authors = b._parse_repo_churn(out)
    assert commits == 2
    assert added == 18
    assert deleted == 3
    assert authors == {"Alice", "Bob"}


def test_parse_counts_a_merge_commit_as_one_commit_zero_lines():
    # merge commit 的 numstat 是空的——照樣算一筆提交、零行。
    out = _repo_output(_commit("Alice"))
    commits, added, deleted, authors = b._parse_repo_churn(out)
    assert (commits, added, deleted) == (1, 0, 0)
    assert authors == {"Alice"}


def test_parse_treats_binary_dash_fields_as_zero():
    out = _repo_output(_commit("Alice", ("-", "-", "assets/logo.png"),
                               ("4", "1", "src/x.py")))
    commits, added, deleted, _ = b._parse_repo_churn(out)
    assert (commits, added, deleted) == (1, 4, 1)


def test_parse_tolerates_blank_and_malformed_lines():
    out = ("\x1fAlice\n"
           "\n"                       # 空行
           "7\t3\tsrc/x.py\n"
           "garbage line without tabs\n"   # 不是 numstat，忽略
           "\t\t\n")                  # 欄位空，忽略
    commits, added, deleted, _ = b._parse_repo_churn(out)
    assert (commits, added, deleted) == (1, 7, 3)


def test_parse_empty_output_is_all_zero():
    commits, added, deleted, authors = b._parse_repo_churn("")
    assert (commits, added, deleted) == (0, 0, 0)
    assert authors == set()


# --------------------------------------------------------------------------
# _build_churn_report
# --------------------------------------------------------------------------
def _rows(**repo_to_output):
    return list(repo_to_output.items())


def test_build_totals_and_per_repo_breakdown():
    rows = _rows(
        repoA=_repo_output(_commit("Alice", ("10", "2", "a")),
                           _commit("Alice", ("5", "5", "b"))),
        repoB=_repo_output(_commit("Bob", ("3", "1", "c"))),
    )
    rep = b._build_churn_report(rows)
    assert rep.total_commits == 3
    assert rep.total_added == 18
    assert rep.total_deleted == 8
    assert rep.authors == 2
    assert rep.scanned == 2
    assert rep.skipped == 0
    names = [r.name for r in rep.repos]
    assert names == ["repoA", "repoB"]   # repoA 有更多提交，排前面
    a = next(r for r in rep.repos if r.name == "repoA")
    assert (a.commits, a.added, a.deleted) == (2, 15, 7)


def test_build_only_lists_repos_with_activity_today():
    rows = _rows(
        active=_repo_output(_commit("Alice", ("1", "0", "a"))),
        idle="",                       # 今天零提交——不該出現在明細
    )
    rep = b._build_churn_report(rows)
    assert rep.scanned == 2
    assert [r.name for r in rep.repos] == ["active"]
    assert rep.total_commits == 1


def test_build_sorts_by_commits_then_churn_then_name():
    rows = _rows(
        low=_repo_output(_commit("A", ("1", "0", "a"))),
        hi=_repo_output(_commit("A", ("1", "0", "a")),
                        _commit("A", ("1", "0", "b"))),
        mid_big=_repo_output(_commit("A", ("100", "50", "a"))),
        mid_small=_repo_output(_commit("A", ("1", "0", "a"))),
    )
    rep = b._build_churn_report(rows)
    # hi(2 提交) > mid_big(1 提交,大 churn) > mid_small(1,小,名字在前) > low(1,小)
    assert [r.name for r in rep.repos] == ["hi", "mid_big", "low", "mid_small"]


def test_build_counts_unreadable_repos_as_skipped_not_zero():
    rows = _rows(
        good=_repo_output(_commit("Alice", ("2", "1", "a"))),
    )
    rows.append(("bad", None))         # 讀不到／git 出錯／逾時
    rep = b._build_churn_report(rows)
    assert rep.scanned == 2
    assert rep.skipped == 1
    assert [r.name for r in rep.repos] == ["good"]
    assert rep.total_commits == 1


def test_build_empty_scan_is_empty_report():
    rep = b._build_churn_report([])
    assert rep.repos == ()
    assert rep.total_commits == 0
    assert rep.scanned == 0
    assert rep.skipped == 0


# --------------------------------------------------------------------------
# _render_churn_report
# --------------------------------------------------------------------------
def test_render_no_activity():
    rep = b._build_churn_report([("idle", "")])
    text = b._render_churn_report(rep)
    assert "今日還沒有任何 git 提交" in text
    assert "idle" not in text          # 沒活動的 repo 不出現


def test_render_no_activity_mentions_skips():
    rep = b._build_churn_report([("bad", None)])
    text = b._render_churn_report(rep)
    assert "今日還沒有任何 git 提交" in text
    assert "1" in text                 # 略過數


def test_render_shows_total_and_breakdown():
    rows = _rows(
        repoA=_repo_output(_commit("Alice", ("10", "2", "a"))),
        repoB=_repo_output(_commit("Bob", ("3", "1", "b"))),
    )
    text = b._render_churn_report(b._build_churn_report(rows))
    assert "**2**" in text             # 總提交數
    assert "+13 / -3" in text          # 總增刪
    assert "`repoA`" in text and "`repoB`" in text
    assert "2 位作者" in text


def test_render_never_leaks_author_names_or_subjects():
    # 作者名（可能是本名）只計人數、絕不印出；render 只用 name/commits/added/deleted，
    # 從不碰 commit 主旨——這裡用一個好認的字串當作者名，確認它不外洩。
    secret = "SECRET_AUTHOR_abc123"
    rows = [("repoA", _repo_output(_commit(secret, ("1", "0", "a"))))]
    text = b._render_churn_report(b._build_churn_report(rows))
    assert secret not in text
    assert "1 位作者" in text


def test_render_stays_under_discord_limit_when_huge():
    rows = [(f"repo_{i:03d}", _repo_output(_commit("A", ("9", "9", "f"))))
            for i in range(400)]
    text = b._render_churn_report(b._build_churn_report(rows))
    assert len(text) < 2000
    assert "已省略" in text            # 有截尾提示


# --------------------------------------------------------------------------
# _scan_workspace_churn（碰磁碟那一半：用注入的 runner ＋ tmp 目錄）
# --------------------------------------------------------------------------
def _make_repo(parent: Path, name: str) -> Path:
    d = parent / name
    (d / ".git").mkdir(parents=True)
    return d


def test_scan_skips_non_git_dirs_and_includes_project_root(monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = _make_repo(ws, "TheBot")
    _make_repo(ws, "OtherRepo")
    (ws / "not_a_repo").mkdir()        # 沒有 .git——要被略過
    (ws / "loose_file.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(b, "PROJECT_ROOT", proj)

    seen = []

    def fake_runner(repo_dir):
        seen.append(repo_dir.name)
        return _repo_output(_commit("A", ("1", "0", "f")))

    rows = b._scan_workspace_churn("2026-09-24 00:00:00", runner=fake_runner)
    names = sorted(n for n, _ in rows)
    assert names == ["OtherRepo", "TheBot"]
    assert "not_a_repo" not in names


def test_scan_degrades_when_runner_raises(monkeypatch, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = _make_repo(ws, "TheBot")
    monkeypatch.setattr(b, "PROJECT_ROOT", proj)

    def boom(repo_dir):
        raise RuntimeError("git blew up")

    rows = b._scan_workspace_churn("2026-09-24 00:00:00", runner=boom)
    assert rows == [("TheBot", None)]          # 例外 → None，不 raise
    rep = b._build_churn_report(rows)
    assert rep.skipped == 1


def test_an_unreadable_workspace_root_is_reported_not_read_as_quiet(monkeypatch, tmp_path):
    """列不出工作區根目錄時只掃得到自己這個 repo。報告要說「有資料夾讀不到」——否則讀起來
    就是「其他 repo 今天都沒有提交」。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = _make_repo(ws, "TheBot")
    monkeypatch.setattr(b, "PROJECT_ROOT", proj)
    real_iterdir = Path.iterdir

    def _iterdir(self):
        if self == ws:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    rows = b._scan_workspace_churn("2026-09-24 00:00:00",
                                   runner=lambda d: _repo_output(_commit("A", ("1", "0", "f"))))
    assert rows == [("TheBot", rows[0][1]), ("workspace", None)], rows
    rep = b._build_churn_report(rows)
    assert rep.skipped == 1 and rep.total_commits == 1
    assert "讀不到" in b._render_churn_report(rep)


def test_a_folder_that_vanishes_mid_scan_is_not_counted_as_unreadable(monkeypatch, tmp_path):
    """列舉途中消失的資料夾是常態，不算讀不到——否則「讀不到」天天出現、沒人會再看。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = _make_repo(ws, "TheBot")
    gone = _make_repo(ws, "Gone")
    monkeypatch.setattr(b, "PROJECT_ROOT", proj)
    real_is_dir = Path.is_dir

    def _is_dir(self):
        if self == gone:
            raise FileNotFoundError("vanished")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)
    rows = b._scan_workspace_churn("2026-09-24 00:00:00", runner=lambda d: "")
    assert rows == [("TheBot", "")], rows


def test_local_midnight_since_shape():
    since = b._local_midnight_since()
    # 形如 YYYY-MM-DD 00:00:00
    assert len(since) == 19
    assert since.endswith(" 00:00:00")
    y, m, d = since.split(" ")[0].split("-")
    assert len(y) == 4 and len(m) == 2 and len(d) == 2


def test_repo_row_skips_linked_worktrees(tmp_path):
    # A linked git worktree's `.git` is a FILE pointing into another repo's
    # /worktrees/; its commits belong to that repo, so churn must skip it to
    # avoid double-counting. A normal repo (.git dir) is still scanned.
    wt = tmp_path / "wt_sc"
    wt.mkdir()
    (wt / ".git").write_text(
        "gitdir: D:/Codes/Imervue/.git/worktrees/wt_sc\n", encoding="utf-8")
    assert b._churn_repo_row(wt, lambda d: "should-not-run") is None

    normal = tmp_path / "normal"
    (normal / ".git").mkdir(parents=True)
    assert b._churn_repo_row(normal, lambda d: "OUT") == ("normal", "OUT")
