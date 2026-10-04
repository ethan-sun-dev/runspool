"""Article parsing and layout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from runspool_wechat.article import ArticleError, load_article, parse_article
from runspool_wechat.render import leaf, render

TEMPLATE = """# 主标题

<!-- card: status draft -->

## 备选标题

1. 另一个标题

## 摘要

这是摘要。

---

第一段。

【配图：对比表】
"""


def test_the_draft_template_yields_title_digest_and_body_only():
    article = parse_article(TEMPLATE, Path("/x/稿件.md"))
    assert (article.title, article.digest) == ("主标题", "这是摘要。")
    assert article.body.startswith("第一段。")
    assert "备选标题" not in article.body and "card" not in article.body
    assert article.placeholders == ["对比表"]


def test_front_matter_and_a_summary_file(tmp_path):
    (tmp_path / "wechat.md").write_text(
        "---\nauthor: Ethan\ncover: images/c.png\n---\n# Title\n\nBody text.\n", encoding="utf-8"
    )
    (tmp_path / "summary.md").write_text("A summary\nacross lines.\n", encoding="utf-8")
    article = load_article(tmp_path / "wechat.md")
    assert (article.author, article.cover, article.digest) == (
        "Ethan",
        "images/c.png",
        "A summary across lines.",
    )


def test_an_article_needs_a_title():
    with pytest.raises(ArticleError, match="no title"):
        parse_article("just text\n", Path("/x/a.md"))


def test_every_piece_of_text_is_a_leaf_and_styles_are_inline():
    html = render("# H\n\nA *b* `c` **d**\n\n> q\n\n1. one\n").html
    stripped = re.sub(r'<span leaf="">[^<]*</span>', "", html)
    assert re.search(r">[^<\s]+<", stripped) is None  # no bare text anywhere
    assert "class=" not in html and "<style" not in html
    assert "\n" not in html  # no whitespace between tags


def test_external_links_become_numbered_references():
    r = render("[docs](https://example.com) and [post](https://mp.weixin.qq.com/s/abc)")
    assert r.external_links == [("docs", "https://example.com")]
    assert '<a href="https://mp.weixin.qq.com/s/abc"' in r.html
    assert '<a href="https://example.com"' not in r.html
    assert leaf("[1]") in r.html and leaf("参考链接") in r.html


def test_raw_html_is_escaped_and_code_keeps_its_shape():
    r = render("<img src=x onerror=alert(1)>\n\n```\n  indented\nnext\n```\n")
    assert "<img src=x" not in r.html and "&lt;img" in r.html
    assert "&nbsp;&nbsp;indented<br>next" in r.html


def test_images_are_resolved_and_captioned():
    r = render("![A chart](img/a.png)", resolve_image=lambda s: "https://cdn/" + s)
    assert r.images == ["img/a.png"]
    assert 'src="https://cdn/img/a.png"' in r.html and leaf("A chart") in r.html


def test_theme_overrides_apply():
    assert "color:red" in render("text", theme={"p": "color:red;"}).html


def test_a_soft_break_after_inline_markup_keeps_the_space():
    assert leaf(" ") in render("Hello **world**\nagain").html
    r = render("see [docs](https://example.com)\nnow")
    assert '<span leaf=""> </span><span leaf="">now</span>' in r.html


def test_table_alignment_survives_styling():
    html = render("| a | b |\n|:-:|--:|\n| 1 | 2 |\n").html
    assert "text-align:center" in html and "text-align:right" in html


def test_references_show_readable_urls():
    r = render("[中文](https://example.com/中文)")
    assert "https://example.com/中文" in r.html


def test_bad_front_matter_is_an_article_error():
    with pytest.raises(ArticleError):
        parse_article("---\ntitle: [unclosed\n---\n# T\n", Path("/x/a.md"))
