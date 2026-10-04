"""The two steps: render an article, then create (or update) its draft.

``wechat_render`` lays the article out and checks it against the draft API's limits;
``wechat_draft`` uploads its images and cover and saves the draft. Creating a draft
puts content on WeChat's servers, so ``wechat_draft`` is a step with side effects:
RunSpool asks a human to approve each attempt before it runs. The draft is never
sent to followers; that stays a decision made in the WeChat back end.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from runspool.builtin_steps.workspace import task_workspace
from runspool.engine.step import Step, StepContext, StepResult
from runspool_wechat.api import WeChatClient
from runspool_wechat.article import Article, load_article
from runspool_wechat.render import render

TITLE_MAX = 32  # characters, per the draft API
DIGEST_MAX = 120
IMAGE_MAX_BYTES = 1024 * 1024  # media/uploadimg accepts jpg/png under 1 MB
COVER_NAMES = ("cover.png", "cover.jpg", "cover.jpeg", "images/cover.png", "images/cover.jpg")


def make_client(appid: str, appsecret: str) -> WeChatClient:
    """Build the API client (replaced in tests)."""
    return WeChatClient(appid, appsecret)


def article_problems(article: Article) -> list[str]:
    problems = []
    if len(article.title) > TITLE_MAX:
        problems.append(f"title has {len(article.title)} characters (limit {TITLE_MAX})")
    if article.digest and len(article.digest) > DIGEST_MAX:
        problems.append(f"digest has {len(article.digest)} characters (limit {DIGEST_MAX})")
    if article.placeholders:
        problems.append(f"{len(article.placeholders)} image placeholder(s) left")
    return problems


def recorded_problems(config: Any, task: Any) -> list[str]:
    """The problems ``wechat_render`` recorded for this task's article, if any."""
    path = task_workspace(config, dict(task)) / "wechat" / "article.json"
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("problems", []))


def local_image(src: str, base_dir: Path) -> Path | None:
    if src.startswith(("http://", "https://", "data:")):
        return None
    return (base_dir / unquote(src)).resolve()


def preview_page(article: Article, body_html: str, problems: list[str]) -> str:
    def esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    notes = "".join(f"<li>{esc(p)}</li>" for p in problems)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview · {esc(article.title)}</title>
<style>
body{{margin:0;background:#eef0f3;font-family:-apple-system,'PingFang SC',sans-serif}}
.wrap{{max-width:420px;margin:20px auto 60px}}
.notes{{background:#fff7e6;border-radius:8px;padding:10px 14px 10px 30px;font-size:13px}}
.phone{{background:#fff;border-radius:12px;padding:20px 16px 32px}}
h1{{margin:0 0 10px;font-size:22px}} .digest{{font-size:13px;color:#666;margin-bottom:20px}}
</style></head><body><div class="wrap">
{f'<ul class="notes">{notes}</ul>' if notes else ""}
<div class="phone"><h1>{esc(article.title)}</h1>
<div class="digest">{esc(article.digest or "")}</div>{body_html}</div>
</div></body></html>
"""


class WeChatRenderStep(Step):
    name = "wechat_render"

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def run(self, ctx: StepContext) -> StepResult:
        article = load_article(Path(ctx.task["input"]).expanduser())
        out = task_workspace(ctx.config, ctx.task) / "wechat"
        out.mkdir(parents=True, exist_ok=True)
        rendered = render(
            article.body,
            resolve_image=lambda src: (
                p.as_uri() if (p := local_image(src, article.base_dir)) else src
            ),
            theme=self._settings.theme,
            references_label=self._settings.references_label,
        )
        problems = article_problems(article)
        (out / "preview.html").write_text(
            preview_page(article, rendered.html, problems), encoding="utf-8"
        )
        summary = {
            "source": str(article.source_path),
            "title": article.title,
            "digest": article.digest,
            "images": rendered.images,
            "external_links": len(rendered.external_links),
            "problems": problems,
        }
        (out / "article.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        updates = {} if ctx.task.get("name") else {"name": article.title}
        if problems:
            return StepResult(message="; ".join(problems), updates=updates, degraded=True)
        return StepResult(message=f"rendered {article.title!r}", updates=updates)


class WeChatDraftStep(Step):
    name = "wechat_draft"
    side_effect = True  # uploads content to WeChat: approved per attempt

    def __init__(self, settings: Any, credentials: Any) -> None:
        self._settings = settings
        self._credentials = credentials

    def run(self, ctx: StepContext) -> StepResult:
        out = task_workspace(ctx.config, ctx.task) / "wechat"
        summary = json.loads((out / "article.json").read_text(encoding="utf-8"))
        if summary["problems"] and not self._settings.allow_problems:
            raise RuntimeError(
                "article has problems (set allow_problems to draft anyway): "
                + "; ".join(summary["problems"])
            )
        article = load_article(summary["source"])
        client = make_client(*self._account())
        cache_path = out / "upload-cache.json"
        cache = (
            json.loads(cache_path.read_text(encoding="utf-8"))
            if cache_path.exists()
            else {"images": {}, "covers": {}}
        )

        def save_cache() -> None:
            cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")

        urls: dict[str, str] = {}
        for src in dict.fromkeys(summary["images"]):
            path = local_image(src, article.base_dir)
            if path is None:
                continue  # remote images are left as they are
            key = _digest(path)
            if key not in cache["images"]:
                _check_uploadable(path)
                ctx.heartbeat(f"uploading {path.name}")
                cache["images"][key] = client.upload_content_image(path)
                save_cache()
            urls[src] = cache["images"][key]

        cover = self._cover(article)
        cover_key = _digest(cover)
        if cover_key not in cache["covers"]:
            ctx.heartbeat("uploading the cover")
            cache["covers"][cover_key] = client.add_image_material(cover)
            save_cache()

        rendered = render(
            article.body,
            resolve_image=lambda src: urls.get(src, src),
            theme=self._settings.theme,
            references_label=self._settings.references_label,
        )
        draft = {
            "title": article.title,
            "author": article.author or self._settings.author,
            "digest": article.digest or "",
            "content": rendered.html,
            "thumb_media_id": cache["covers"][cover_key],
            "need_open_comment": 1 if self._settings.open_comment else 0,
            "only_fans_can_comment": 0,
        }
        metadata = dict(ctx.task.get("metadata") or {})
        media_id = metadata.get("wechat_media_id")
        if media_id:
            client.update_draft(media_id, draft)
            verb = "updated"
        else:
            media_id = client.add_draft(draft)
            verb = "created"
        metadata["wechat_media_id"] = media_id
        (out / "draft.json").write_text(
            json.dumps({"media_id": media_id, "title": article.title}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return StepResult(
            message=(
                f"draft {verb} ({media_id}); the API cannot declare original content: "
                "turn it on in the WeChat back end before sending"
            ),
            updates={"metadata": metadata},
        )

    def _account(self) -> tuple[str, str]:
        settings = self._settings
        appid = settings.appid or self._credentials.resolve(settings.appid_credential)
        secret = self._credentials.resolve(settings.appsecret)
        missing = [
            name
            for name, value in (
                (settings.appid_credential, appid),
                (settings.appsecret, secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"WeChat credentials not configured: {', '.join(missing)}")
        return appid, secret

    def _cover(self, article: Article) -> Path:
        candidates = [article.cover] if article.cover else list(COVER_NAMES)
        for name in candidates:
            path = (article.base_dir / name).resolve()
            if path.is_file():
                return path
        raise RuntimeError(
            "no cover image: set `cover:` in the front matter, or put cover.png next to "
            "the article (2.35:1, e.g. 900x383)"
        )


def _digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _check_uploadable(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"image not found: {path}")
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise RuntimeError(f"{path.name}: WeChat body images must be jpg or png")
    if path.stat().st_size >= IMAGE_MAX_BYTES:
        raise RuntimeError(f"{path.name}: WeChat body images must be under 1 MB")
