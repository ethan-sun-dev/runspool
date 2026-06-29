"""summarize_text: produce a short extractive summary of the normalized document.

Offline and deterministic: counts, leading sentences, and top keywords by
frequency. No model calls. The point is to demonstrate a step that consumes a
prior step's artifact and emits a human-facing one.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from runspool.builtin_steps.workspace import task_workspace
from runspool.engine.step import Step, StepContext, StepResult

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")

# Common words excluded from keyword ranking.
_STOPWORDS = frozenset(
    """the a an and or but of to in on for with at by from as is are was were be been
    being this that these those it its we you they he she them our your their i me my
    will would can could should may might must shall not no yes do does did have has had
    if then else when while which who whom whose what where why how all any each more
    most some such than too very just about into over under again further once""".split()
)


def summarize(text: str, *, max_sentences: int = 3, top_keywords: int = 8) -> dict:
    # Drop Markdown heading lines from sentence selection.
    body_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = " ".join(ln.strip() for ln in body_lines if ln.strip())
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(body) if s.strip()]
    lead = sentences[:max_sentences]

    words = [w.lower() for w in _WORD.findall(text)]
    meaningful = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    keywords = [w for w, _ in Counter(meaningful).most_common(top_keywords)]

    return {
        "word_count": len(words),
        "sentence_count": len(sentences),
        "summary_sentences": lead,
        "keywords": keywords,
    }


def render_markdown(summary: dict, *, title: str) -> str:
    lines = [f"# Summary: {title}", ""]
    lines.append(f"- Words: {summary['word_count']}")
    lines.append(f"- Sentences: {summary['sentence_count']}")
    if summary["keywords"]:
        lines.append(f"- Keywords: {', '.join(summary['keywords'])}")
    lines.append("")
    lines.append("## Lead")
    lines.append("")
    if summary["summary_sentences"]:
        for s in summary["summary_sentences"]:
            lines.append(f"- {s}")
    else:
        lines.append("- (no extractable sentences)")
    return "\n".join(lines) + "\n"


class TextSummarizeStep(Step):
    name = "summarize_text"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        normalized_path = ws / "normalized.md"
        source = normalized_path if normalized_path.exists() else ws / "source.txt"
        text = source.read_text(encoding="utf-8")
        title = ctx.task.get("name") or "Document"

        summary = summarize(text)
        (ws / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (ws / "summary.md").write_text(render_markdown(summary, title=title), encoding="utf-8")
        return StepResult(message=f"summarized {summary['word_count']} words")
