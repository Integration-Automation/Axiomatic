"""`_reply_card.card`：平台的長度上限一次處理，放不下的要講出來。

平台對超過上限的嵌入訊息是**整則拒收**，而那會是一則安靜消失的回覆——所以截斷與「另有幾項放不下」
都是正確性，不是美觀。
"""
from __future__ import annotations

import _reply_card as rc
from _chat_platform import flatten_embed


def _total(embed) -> int:
    size = len(embed.title or "") + len(embed.description or "")
    size += sum(len(f.name) + len(f.value) for f in embed.fields)
    return size + len(getattr(embed.footer, "text", None) or "")


def test_status_picks_the_color_and_unknown_falls_back_to_info():
    assert rc.card("x", status="bad").color.value == rc.STATUS_COLORS["bad"]
    assert rc.card("x", status="nonsense").color.value == rc.STATUS_COLORS["info"]


def test_long_parts_are_clipped_with_an_ellipsis():
    embed = rc.card("t" * 400, description="d" * 5000,
                    fields=[("n" * 300, "v" * 2000)])
    assert len(embed.title) == rc.TITLE_LIMIT and embed.title.endswith("…")
    assert len(embed.fields[0].name) == rc.FIELD_NAME_LIMIT
    assert len(embed.fields[0].value) == rc.FIELD_VALUE_LIMIT
    assert _total(embed) <= rc.TOTAL_LIMIT


def test_fields_that_do_not_fit_are_counted_in_the_footer():
    embed = rc.card("t", fields=[(f"f{i}", "v") for i in range(30)], footer="來源說明")
    assert len(embed.fields) == rc.FIELD_LIMIT
    assert "另有 5 項放不下" in embed.footer.text and embed.footer.text.startswith("來源說明")


def test_the_whole_card_stays_under_the_platform_total():
    embed = rc.card("t", fields=[(f"f{i}", "x" * 1000) for i in range(20)])
    assert _total(embed) <= rc.TOTAL_LIMIT
    assert "放不下" in embed.footer.text


def test_empty_names_and_values_are_filled_so_the_platform_accepts_them():
    embed = rc.card("t", fields=[("", ""), ("名稱", "值", True)])
    assert embed.fields[0].value == "—" and embed.fields[1].inline is True


def test_a_platform_without_cards_sees_the_same_content():
    flat = flatten_embed(rc.card("標題", description="說明", fields=[("欄", "值")], footer="頁尾"))
    assert flat.splitlines() == ["標題", "說明", "欄: 值", "頁尾"]


def test_drop_mode_omits_an_oversized_field_whole_and_says_so():
    embed = rc.card("t", fields=[("a", "ok"), ("b", "z" * 2000), ("c", "ok")],
                    overflow="drop", omitted=lambda n: f"少了 {n} 項")
    assert [f.name for f in embed.fields] == ["a", "c"]
    assert embed.footer.text == "少了 1 項"
    assert "zzz" not in flatten_embed(embed)


def test_bullet_reports_become_fields_and_take_the_worst_status():
    lines = ["- **背景產圖程式**: ✅ running", "- **disk**: ⚠️ low", "a free line"]
    embed = rc.from_bullets("🩺 健康檢查", lines)
    assert [(f.name, f.value) for f in embed.fields] == [
        ("背景產圖程式", "✅ running"), ("disk", "⚠️ low")]
    assert embed.description == "a free line"
    assert embed.color.value == rc.STATUS_COLORS["warn"]
    assert rc.bullets_status(["- **x**: (err — 請查看 log)"]) == "bad"
    assert rc.bullets_status(["- **x**: ✅"]) == "ok"
