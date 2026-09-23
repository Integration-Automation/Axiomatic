"""Sphinx 設定檔 — Axiomatic 文件（繁體中文）。

以 MyST-Parser 直接用 Markdown 撰寫，主題用 sphinx_rtd_theme（Read the
Docs 經典外觀）。本機建置：

    py -3 -m pip install -r docs/requirements.txt
    py -3 -m sphinx -b html docs docs/_build/html
"""
from __future__ import annotations

# -- 專案資訊 ----------------------------------------------------------------
project = "Axiomatic"
author = "Axiomatic contributors"
copyright = "2026, Axiomatic contributors"
release = "1.0"

# -- 一般設定 ----------------------------------------------------------------
extensions = [
    "myst_parser",
]

# MyST 擴充語法：::: 圍欄、定義清單、待辦清單等
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "tasklist",
    "substitution",
]
myst_heading_anchors = 3

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# 介面語言（影響搜尋斷詞、自動產生的字串）
language = "zh_TW"

source_suffix = {
    ".md": "markdown",
    ".rst": "restructuredtext",
}

# -- HTML 輸出 ---------------------------------------------------------------
html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = "Axiomatic 文件"

html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": False,
    "style_external_links": True,
}
