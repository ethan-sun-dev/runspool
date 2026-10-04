"""Markdown -> HTML for the WeChat Official Account editor.

The editor keeps only inline styles (it drops ``<style>`` and ``class``), so every
element carries its style. Other platform behaviour this renderer accounts for:

* visible text is stored as ``<span leaf="">``; bare text loses its style when the
  article is edited again in the WeChat back end, so all text goes through
  :func:`leaf`;
* only links to WeChat articles are clickable in an article body: other links are
  rendered as text with a numbered marker, and listed as references at the end;
* the editor turns whitespace between block tags into content (empty list items),
  so the output has none;
* ``<pre>`` loses its line breaks and leading spaces, so code is rendered with
  ``<br>`` and ``&nbsp;``;
* raw HTML in the Markdown is escaped, never passed through.

Images are resolved through a callback: ``file://`` URLs for a local preview, the
URLs returned by the upload API for a draft.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

# A neutral default; a profile overrides any key through the plugin's ``theme`` config.
DEFAULT_THEME: dict[str, str] = {
    "root": "padding:0 4px;font-size:15px;line-height:1.75;color:#333;"
    "letter-spacing:0.5px;text-align:left;word-break:break-word;",
    "p": "margin:0 0 16px;text-align:left;",
    "h1": "margin:32px 0 16px;font-size:22px;line-height:1.4;font-weight:700;color:#111;",
    "h2": "margin:32px 0 16px;font-size:19px;line-height:1.45;font-weight:700;color:#111;",
    "h3": "margin:24px 0 12px;padding-left:10px;border-left:4px solid #576b95;"
    "font-size:17px;line-height:1.5;font-weight:700;color:#111;",
    "h4": "margin:20px 0 10px;font-size:15px;font-weight:700;color:#111;",
    "blockquote": "margin:0 0 16px;padding:12px 16px;background:#f5f6f8;"
    "border-left:3px solid #576b95;color:#444;",
    "blockquote_p": "margin:0 0 4px;",
    "ul": "margin:0 0 16px;padding-left:20px;",
    "ol": "margin:0 0 16px;padding-left:20px;",
    "li": "margin:0 0 6px;",
    "strong": "font-weight:700;color:#111;",
    "em": "font-style:italic;",
    "del": "color:#999;",
    "a": "color:#576b95;text-decoration:none;",
    "link_text": "color:#576b95;",
    "sup": "font-size:11px;color:#576b95;",
    "code_inline": "padding:2px 4px;margin:0 2px;font-size:13px;background:#f5f6f8;"
    "border-radius:3px;font-family:Menlo,Consolas,monospace;",
    "pre": "margin:0 0 16px;padding:12px 14px;background:#f5f6f8;border-radius:6px;"
    "overflow-x:auto;font-size:13px;line-height:1.6;",
    "code": "font-family:Menlo,Consolas,monospace;",
    "hr": "margin:28px 0;text-align:center;color:#bbb;font-size:14px;letter-spacing:10px;",
    "img": "display:block;max-width:100%;height:auto;margin:0 auto;",
    "figcaption": "display:block;margin:8px 0 18px;font-size:12px;line-height:1.6;"
    "color:#888;text-align:center;",
    "table_wrap": "margin:0 0 16px;overflow-x:auto;",
    "table": "border-collapse:collapse;width:100%;font-size:13px;line-height:1.6;",
    "th": "padding:6px 8px;border:1px solid #e5e5e5;background:#f5f6f8;font-weight:700;",
    "td": "padding:6px 8px;border:1px solid #e5e5e5;vertical-align:top;",
    "refs_title": "margin:32px 0 10px;font-size:13px;font-weight:700;color:#888;",
    "refs": "margin:0;padding-left:20px;font-size:12px;line-height:1.7;color:#888;"
    "word-break:break-all;",
}

_BLOCK_STYLE = {
    "paragraph_open": "p",
    "blockquote_open": "blockquote",
    "bullet_list_open": "ul",
    "ordered_list_open": "ol",
    "list_item_open": "li",
    "table_open": "table",
    "th_open": "th",
    "td_open": "td",
}
_INLINE_STYLE = {
    "strong_open": "strong",
    "em_open": "em",
    "s_open": "del",
    "code_inline": "code_inline",
}


@dataclass
class Rendered:
    html: str
    external_links: list[tuple[str, str]] = field(default_factory=list)  # (text, url)
    images: list[str] = field(default_factory=list)  # sources as written in the Markdown


def is_wechat_article(url: str) -> bool:
    return bool(re.match(r"^https?://mp\.weixin\.qq\.com/", url))


def leaf(text: str) -> str:
    return f'<span leaf="">{html.escape(text, quote=False)}</span>' if text else ""


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def render(
    markdown: str,
    *,
    resolve_image: Callable[[str], str] = lambda src: src,
    theme: Mapping[str, str] | None = None,
    references_label: str = "参考链接",
) -> Rendered:
    styles = {**DEFAULT_THEME, **(theme or {})}
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    result = Rendered("")
    env: dict[str, Any] = {"link_stack": []}

    def style_tokens(state: Any) -> None:
        depth = 0
        for token in state.tokens:
            if token.type == "blockquote_open":
                depth += 1
            elif token.type == "blockquote_close":
                depth -= 1
            key = _BLOCK_STYLE.get(token.type)
            if token.type == "heading_open":
                key = token.tag if token.tag in ("h1", "h2", "h3") else "h4"
            if key:
                token.attrSet("style", styles["blockquote_p" if key == "p" and depth else key])
            for child in token.children or []:
                inline_key = _INLINE_STYLE.get(child.type)
                if inline_key:
                    child.attrSet("style", styles[inline_key])

    md.core.ruler.push("wechat_styles", style_tokens)

    def text(self, tokens, idx, options, env):
        return leaf(tokens[idx].content)

    def softbreak(self, tokens, idx, options, env):
        # Between two Latin words keep a space; between CJK characters, nothing.
        before = tokens[idx - 1].content if idx > 0 else ""
        after = tokens[idx + 1].content if idx + 1 < len(tokens) else ""
        latin = re.search(r"[A-Za-z0-9]$", before) and re.match(r"^[A-Za-z0-9]", after)
        return leaf(" ") if latin else ""

    def code_inline(self, tokens, idx, options, env):
        token = tokens[idx]
        return f'<code style="{_attr(token.attrGet("style") or "")}">{leaf(token.content)}</code>'

    def code_block(self, tokens, idx, options, env):
        lines = tokens[idx].content.rstrip("\n").split("\n")
        body = "<br>".join(
            re.sub(r"^ +", lambda m: "&nbsp;" * len(m.group(0)), html.escape(line, quote=False))
            for line in lines
        )
        return (
            f'<pre style="{_attr(styles["pre"])}"><code style="{_attr(styles["code"])}">'
            f"{body}</code></pre>"
        )

    def hr(self, tokens, idx, options, env):
        return f'<section style="{_attr(styles["hr"])}">{leaf("• • •")}</section>'

    def image(self, tokens, idx, options, env):
        token = tokens[idx]
        src = str(token.attrGet("src") or "")
        result.images.append(src)
        alt = token.content
        img = (
            f'<img src="{_attr(resolve_image(src))}" alt="{_attr(alt)}" '
            f'style="{_attr(styles["img"])}">'
        )
        caption = f'<span style="{_attr(styles["figcaption"])}">{leaf(alt)}</span>' if alt else ""
        return img + caption

    def link_open(self, tokens, idx, options, env):
        href = str(tokens[idx].attrGet("href") or "")
        if is_wechat_article(href):
            env["link_stack"].append(None)
            return f'<a href="{_attr(href)}" style="{_attr(styles["a"])}">'
        label = "".join(
            t.content for t in _until_close(tokens, idx) if t.type in ("text", "code_inline")
        )
        result.external_links.append((label, href))
        env["link_stack"].append(len(result.external_links))
        return f'<span style="{_attr(styles["link_text"])}">'

    def link_close(self, tokens, idx, options, env):
        number = env["link_stack"].pop()
        if number is None:
            return "</a>"
        return f'</span><sup style="{_attr(styles["sup"])}">{leaf(f"[{number}]")}</sup>'

    def table_open(self, tokens, idx, options, env):
        return f'<section style="{_attr(styles["table_wrap"])}">' + self.renderToken(
            tokens, idx, options, env
        )

    def table_close(self, tokens, idx, options, env):
        return self.renderToken(tokens, idx, options, env) + "</section>"

    for name, rule in {
        "text": text,
        "softbreak": softbreak,
        "code_inline": code_inline,
        "code_block": code_block,
        "fence": code_block,
        "hr": hr,
        "image": image,
        "link_open": link_open,
        "link_close": link_close,
        "table_open": table_open,
        "table_close": table_close,
    }.items():
        md.add_render_rule(name, rule)

    inner = re.sub(r">\s*\n\s*<", "><", md.render(markdown, env)).strip()
    if result.external_links:
        items = "".join(
            f"<li>{leaf((label + ': ' if label and label != url else '') + url)}</li>"
            for label, url in result.external_links
        )
        inner += (
            f'<p style="{_attr(styles["refs_title"])}">{leaf(references_label)}</p>'
            f'<ol style="{_attr(styles["refs"])}">{items}</ol>'
        )
    result.html = f'<section style="{_attr(styles["root"])}">{inner}</section>'
    return result


def _until_close(tokens: list[Token], open_idx: int) -> list[Token]:
    out = []
    for token in tokens[open_idx + 1 :]:
        if token.type == "link_close":
            break
        out.append(token)
    return out
