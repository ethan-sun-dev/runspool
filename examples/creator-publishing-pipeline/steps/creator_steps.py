"""Custom steps for the creator-publishing-pipeline example.

Offline and draft-only: from raw materials (notes, a transcript, assets) it
builds a multi-platform content package you can review before publishing. It
never publishes anything — by design it produces drafts and a checklist.

The task ``input`` is the path to a materials directory::

    materials/
      notes.md
      source.md
      assets/*

Output (under the task workspace, archived on completion)::

    dist/
      article.md
      wechat.html
      x-thread.md
      linkedin-post.md
      bilibili-description.md
      publish-checklist.md
      manifest.json
      assets/
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from runspool.builtin_steps.workspace import task_workspace
from runspool.engine.step import Step, StepContext, StepResult

_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _materials(ws: Path) -> Path:
    return ws / "materials"


def _dist(ws: Path) -> Path:
    d = ws / "dist"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("# "):
            return line.strip("# ").strip()
    return fallback


class CollectMaterialsStep(Step):
    name = "collect_materials"

    def run(self, ctx: StepContext) -> StepResult:
        root = Path(ctx.task["input"]).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(f"input must be a materials directory: {root}")
        ws = task_workspace(ctx.config, ctx.task)
        dest = _materials(ws)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root, dest)
        index = [
            str(p.relative_to(dest))
            for p in sorted(dest.rglob("*"))
            if p.is_file()
        ]
        (ws / "materials-index.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return StepResult(message=f"collected {len(index)} material files")


class ExtractHighlightsStep(Step):
    name = "extract_highlights"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        mats = _materials(ws)
        notes = _read(mats / "notes.md")
        source = _read(mats / "source.md")

        highlights = [m.group(1).strip() for m in map(_BULLET.match, notes.splitlines()) if m]
        if not highlights:
            # Fall back to the first few sentences of the source transcript.
            sentences = [s.strip() for s in _SENTENCE.split(source) if s.strip()]
            highlights = sentences[:5]
        (ws / "highlights.json").write_text(
            json.dumps(highlights, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return StepResult(message=f"{len(highlights)} highlights")


class DraftArticleStep(Step):
    name = "draft_article"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        mats = _materials(ws)
        source = _read(mats / "source.md")
        highlights = json.loads(_read(ws / "highlights.json") or "[]")
        title = _title(source, ctx.task.get("name") or "Untitled")

        body = [f"# {title}", ""]
        body += ["## Key takeaways", ""]
        body += [f"- {h}" for h in highlights] or ["- (none)"]
        body += ["", "## Article", ""]
        # Use the source body (minus its heading) as the article draft.
        source_body = "\n".join(
            ln for ln in source.splitlines() if not ln.strip().startswith("# ")
        ).strip()
        body.append(source_body or "_Draft body goes here._")
        body.append("")

        _dist(ws).joinpath("article.md").write_text("\n".join(body) + "\n", encoding="utf-8")
        return StepResult(message=f"drafted '{title}'")


class RenderPlatformPackageStep(Step):
    name = "render_platform_package"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        dist = _dist(ws)
        article = _read(dist / "article.md")
        title = _title(article, ctx.task.get("name") or "Untitled")
        highlights = json.loads(_read(ws / "highlights.json") or "[]")

        # WeChat: a minimal self-contained HTML wrapper (no external rendering).
        html_body = article.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        (dist / "wechat.html").write_text(
            "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head>\n<body>\n<pre>{html_body}</pre>\n"
            "</body></html>\n",
            encoding="utf-8",
        )

        # X / Twitter thread from highlights.
        thread = [f"{i + 1}/ {h}" for i, h in enumerate(highlights)] or [f"1/ {title}"]
        (dist / "x-thread.md").write_text("\n\n".join(thread) + "\n", encoding="utf-8")

        # LinkedIn post.
        linkedin = [title, ""]
        linkedin += [f"• {h}" for h in highlights[:3]]
        linkedin += ["", "#automation #localfirst #workflows"]
        (dist / "linkedin-post.md").write_text("\n".join(linkedin) + "\n", encoding="utf-8")

        # Bilibili description.
        desc = [title, "", "In this video:"]
        desc += [f"- {h}" for h in highlights]
        (dist / "bilibili-description.md").write_text("\n".join(desc) + "\n", encoding="utf-8")

        # Copy any assets through to the package.
        src_assets = _materials(ws) / "assets"
        if src_assets.is_dir():
            shutil.copytree(src_assets, dist / "assets", dirs_exist_ok=True)

        return StepResult(message="rendered platform package (drafts only)")


class CreatePublishChecklistStep(Step):
    name = "create_publish_checklist"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        dist = _dist(ws)
        artifacts = sorted(
            str(p.relative_to(dist)) for p in dist.rglob("*") if p.is_file()
        )

        checklist = [
            "# Publish checklist",
            "",
            "> Drafts only. Review each item before publishing manually.",
            "",
            "- [ ] Proofread `article.md`",
            "- [ ] Confirm the WeChat HTML renders correctly",
            "- [ ] Review the X/Twitter thread for length and tone",
            "- [ ] Add the cover image to each platform",
            "- [ ] Schedule or publish on each channel by hand",
            "",
            "## Package contents",
            "",
        ]
        checklist += [f"- {a}" for a in artifacts]
        (dist / "publish-checklist.md").write_text("\n".join(checklist) + "\n", encoding="utf-8")

        manifest = {
            "title": _title(_read(dist / "article.md"), ctx.task.get("name") or "Untitled"),
            "platforms": ["wechat", "x", "linkedin", "bilibili"],
            "auto_publish": False,
            "artifacts": artifacts,
        }
        (dist / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return StepResult(message="publish checklist + manifest ready")
