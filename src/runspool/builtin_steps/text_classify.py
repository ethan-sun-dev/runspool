"""classify_text: assign a coarse category to the source text by keyword match.

Deterministic and offline: a tiny keyword model, not machine learning. It is
meant to show how a step turns input into a structured artifact that later steps
(or an operator) can branch on.
"""

from __future__ import annotations

import json

from runspool.builtin_steps.workspace import read_source, task_workspace
from runspool.engine.step import Step, StepContext, StepResult

# Ordered so the first category with the most matches wins ties predictably.
_CATEGORIES: dict[str, tuple[str, ...]] = {
    "invoice": ("invoice", "amount due", "subtotal", "tax", "total", "bill to", "payment terms"),
    "support_ticket": (
        "ticket",
        "issue",
        "error",
        "bug",
        "cannot",
        "can't",
        "broken",
        "support",
        "reproduce",
    ),
    "meeting_notes": (
        "meeting",
        "agenda",
        "attendees",
        "action item",
        "action items",
        "next steps",
        "minutes",
        "discussed",
    ),
}


def classify(text: str) -> dict:
    lower = text.lower()
    scores: dict[str, list[str]] = {}
    for category, keywords in _CATEGORIES.items():
        hits = [kw for kw in keywords if kw in lower]
        if hits:
            scores[category] = hits
    if not scores:
        return {"category": "general", "confidence": 0.0, "matched_keywords": []}
    best = max(scores, key=lambda c: len(scores[c]))
    hits = scores[best]
    confidence = round(min(len(hits) / 3.0, 1.0), 2)
    return {"category": best, "confidence": confidence, "matched_keywords": hits}


class TextClassifyStep(Step):
    name = "classify_text"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        text = read_source(ws)
        result = classify(text)
        (ws / "classification.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return StepResult(
            message=f"classified as {result['category']} (confidence {result['confidence']})"
        )
