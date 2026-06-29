"""Custom steps for the client-intel-brief example.

These are ordinary ``Step`` subclasses loaded dynamically by Runspool (see this
example's config.yaml). They are fully offline: they read Markdown/YAML sources
from a local directory and assemble a briefing package. No network or API keys.

The task ``input`` is the path to a source directory laid out like::

    data/
      client_profile.md
      requirements.md
      sources.yaml
      competitors/*.md
      customer_notes/*.md
      pricing_pages/*.md
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from runspool.builtin_steps.workspace import task_workspace
from runspool.engine.step import Step, StepContext, StepResult

_REQUIRED = ("client_profile.md", "requirements.md")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def _sources(ws: Path) -> Path:
    return ws / "sources"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _bullets(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = _BULLET.match(line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith("# "):
            return line.strip("# ").strip()
    return fallback


class CollectSourcesStep(Step):
    """Validate the source directory and copy it into the task workspace."""

    name = "collect_sources"

    def run(self, ctx: StepContext) -> StepResult:
        root = Path(ctx.task["input"]).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(f"input must be a source directory: {root}")
        missing = [name for name in _REQUIRED if not (root / name).exists()]
        if missing:
            # This surfaces in `runspool inspect <id>` as manual_required with a
            # clear suggested_next_action once retries are exhausted.
            raise FileNotFoundError(f"Missing required source(s): {', '.join(missing)}")

        ws = task_workspace(ctx.config, ctx.task)
        dest = _sources(ws)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root, dest)

        index = [
            {"path": str(p.relative_to(dest)), "bytes": p.stat().st_size}
            for p in sorted(dest.rglob("*"))
            if p.is_file()
        ]
        (ws / "source-index.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        ctx.heartbeat(f"collected {len(index)} files")
        return StepResult(message=f"collected {len(index)} source files")


class ExtractSignalsStep(Step):
    """Pull bullet-point signals from the profile, requirements, and call notes."""

    name = "extract_signals"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        src = _sources(ws)
        signals: dict[str, list[str]] = {
            "profile": _bullets(_read(src / "client_profile.md")),
            "requirements": _bullets(_read(src / "requirements.md")),
            "customer_notes": [],
        }
        notes_dir = src / "customer_notes"
        if notes_dir.is_dir():
            for note in sorted(notes_dir.glob("*.md")):
                signals["customer_notes"].extend(_bullets(_read(note)))
        (ws / "signals.json").write_text(
            json.dumps(signals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        total = sum(len(v) for v in signals.values())
        return StepResult(message=f"extracted {total} signals")


class MapCompetitorsStep(Step):
    """Summarise each competitor file into a name + bullet list."""

    name = "map_competitors"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        comp_dir = _sources(ws) / "competitors"
        competitors = []
        if comp_dir.is_dir():
            for f in sorted(comp_dir.glob("*.md")):
                text = _read(f)
                competitors.append(
                    {"name": _title(text, f.stem), "notes": _bullets(text)}
                )
        (ws / "competitors.json").write_text(
            json.dumps(competitors, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return StepResult(message=f"mapped {len(competitors)} competitors")


class IdentifyOpportunitiesStep(Step):
    """Cross requirements against competitor coverage to flag opportunities."""

    name = "identify_opportunities"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        signals = json.loads(_read(ws / "signals.json") or "{}")
        competitors = json.loads(_read(ws / "competitors.json") or "[]")
        requirements = signals.get("requirements", [])

        competitor_text = " ".join(
            note.lower() for c in competitors for note in c.get("notes", [])
        )
        lines = ["# Opportunity Map", ""]
        opportunities = []
        for req in requirements:
            key = req.lower().split()[0] if req.split() else ""
            covered = bool(key) and key in competitor_text
            verdict = "covered by competitors" if covered else "OPPORTUNITY (gap)"
            if not covered:
                opportunities.append(req)
            lines.append(f"- {req} — {verdict}")
        lines.append("")
        lines.append(f"_{len(opportunities)} potential gap(s) identified._")
        (ws / "opportunity-map.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return StepResult(message=f"{len(opportunities)} opportunities flagged")


class WriteBriefStep(Step):
    """Assemble the final client-intel-brief.md from prior artifacts."""

    name = "write_brief"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        src = _sources(ws)
        signals = json.loads(_read(ws / "signals.json") or "{}")
        competitors = json.loads(_read(ws / "competitors.json") or "[]")
        profile = _read(src / "client_profile.md")
        client_name = _title(profile, ctx.task.get("name") or "Client")

        out = [f"# Client Intelligence Brief: {client_name}", ""]
        out += ["## Profile", ""]
        out += [f"- {s}" for s in signals.get("profile", [])] or ["- (none)"]
        out += ["", "## Requirements", ""]
        out += [f"- {s}" for s in signals.get("requirements", [])] or ["- (none)"]
        out += ["", "## Voice of customer", ""]
        out += [f"- {s}" for s in signals.get("customer_notes", [])] or ["- (none)"]
        out += ["", "## Competitive landscape", ""]
        for c in competitors:
            out.append(f"### {c['name']}")
            out += [f"- {n}" for n in c.get("notes", [])] or ["- (no notes)"]
            out.append("")
        out += ["## Opportunities", "", _read(ws / "opportunity-map.md").strip(), ""]

        (ws / "client-intel-brief.md").write_text("\n".join(out) + "\n", encoding="utf-8")
        return StepResult(message="brief written")
