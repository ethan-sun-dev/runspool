"""Parse a Markdown article: title, digest, author, cover, body.

Two shapes are understood:

1. A draft template: ``# Title``, then optional ``## Alternative titles`` /
   ``## Summary`` (``## 备选标题`` / ``## 摘要``) sections, then a line ``---``, then
   the body. Only the body is published; the summary becomes the digest.
2. Plain Markdown, optionally with YAML front matter (``title``, ``digest``,
   ``author``, ``cover``).

If no digest is given, a ``summary.md`` next to the file is used. HTML comments are
dropped. Standalone ``【配图：…】`` lines are placeholders for images still to come.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLACEHOLDER = re.compile(r"【配图[:：]([^】]*)】")
_FRONT_MATTER = re.compile(r"^---\n([\s\S]*?)\n---\n")
_SUMMARY_HEADING = re.compile(r"^##\s*(摘要|Summary)", re.IGNORECASE)
_TEMPLATE_HEADING = re.compile(r"^##\s*(备选标题|摘要|Alternative titles|Summary)", re.IGNORECASE)


@dataclass
class Article:
    title: str
    body: str
    source_path: Path
    digest: str | None = None
    author: str | None = None
    cover: str | None = None  # relative to base_dir
    placeholders: list[str] = field(default_factory=list)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent


class ArticleError(ValueError):
    pass


def load_article(path: Path | str) -> Article:
    path = Path(path).resolve()
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArticleError(f"cannot read {path}: {exc}") from exc
    article = parse_article(source, path)
    if not article.digest:
        summary = path.parent / "summary.md"
        if summary.is_file():
            text = _FRONT_MATTER.sub("", summary.read_text(encoding="utf-8"))
            article.digest = " ".join(text.split()) or None
    return article


def parse_article(source: str, source_path: Path) -> Article:
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    front: dict = {}
    match = _FRONT_MATTER.match(text)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ArticleError(f"{source_path}: front matter is not valid YAML") from exc
        front = parsed if isinstance(parsed, dict) else {}
        text = text[match.end() :]
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    lines = text.split("\n")

    h1 = next((i for i, line in enumerate(lines) if re.match(r"^#\s+\S", line)), None)
    title = front.get("title") or (lines[h1][1:].strip() if h1 is not None else None)
    if not title:
        raise ArticleError(f"{source_path}: no title (a '# Title' line, or front matter 'title')")

    after = lines[h1 + 1 :] if h1 is not None else lines
    separator = _template_separator(after)
    digest = front.get("digest")
    if separator is not None:
        if digest is None:
            digest = _section(after[:separator], _SUMMARY_HEADING)
        after = after[separator + 1 :]
    body = "\n".join(after).strip()
    return Article(
        title=str(title).strip(),
        body=body,
        source_path=source_path,
        digest=(str(digest).strip() or None) if digest else None,
        author=front.get("author"),
        cover=front.get("cover"),
        placeholders=[m.group(1).strip() for m in PLACEHOLDER.finditer(body)],
    )


def _template_separator(lines: list[str]) -> int | None:
    """The first ``---`` after the title, if a template heading came before it."""
    seen_heading = False
    in_fence = False
    for i, line in enumerate(lines):
        if re.match(r"^(```|~~~)", line):
            in_fence = not in_fence
        if in_fence:
            continue
        if _TEMPLATE_HEADING.match(line):
            seen_heading = True
        if line.strip() == "---":
            return i if seen_heading else None
    return None


def _section(lines: list[str], heading: re.Pattern[str]) -> str | None:
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    out = []
    for line in lines[start + 1 :]:
        if re.match(r"^#{1,6}\s", line):
            break
        if line.strip():
            out.append(line.strip())
    return " ".join(out) or None
