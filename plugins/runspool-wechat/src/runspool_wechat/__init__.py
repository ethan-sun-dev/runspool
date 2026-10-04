"""runspool-wechat: the official RunSpool plugin for WeChat Official Accounts.

Mount it with the ``wechat`` bundle, or insert the ``wechat`` plugin yourself::

    bundles: [core, builtin-steps, wechat]
    patch:
      - id: wechat
        config: {author: "Your name"}

It contributes:

* the steps ``wechat_render`` (lay out Markdown, check it) and ``wechat_draft``
  (upload images and cover, save the draft; it has side effects, so each attempt
  waits for ``runspool approve``), and the workflow ``wechat_article`` running both;
* the commands ``runspool wechat token`` (check credentials and the IP whitelist)
  and ``runspool wechat preview <article.md>``;
* a doctor check that the account's credentials are configured.

Credentials are names, resolved through RunSpool's ``credentials`` service:
``WECHAT_APPSECRET`` (and ``WECHAT_APPID``, unless ``appid`` is set in config).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from runspool.core.credentials import CredentialRef
from runspool.engine.gate import Deny
from runspool.kernel import Plugin
from runspool_wechat import steps as _steps
from runspool_wechat.article import load_article
from runspool_wechat.render import render

__all__ = ["BUNDLE", "WechatConfig", "plugin"]


class WechatConfig(BaseModel):
    appid: str | None = None  # not a secret; or leave unset and use appid_credential
    appid_credential: CredentialRef = "WECHAT_APPID"
    appsecret: CredentialRef = "WECHAT_APPSECRET"
    author: str = ""
    open_comment: bool = True
    allow_problems: bool = False  # draft even with over-long titles, placeholders...
    theme: dict[str, str] = {}  # style overrides, see runspool_wechat.render.DEFAULT_THEME
    references_label: str = "参考链接"
    workflow: str = "wechat_article"


def apply(ctx, config: WechatConfig) -> None:
    ctx.steps.register(_steps.WeChatRenderStep(config))
    ctx.steps.register(_steps.WeChatDraftStep(config, ctx.credentials))
    ctx.workflows.add_default(config.workflow, ["wechat_render", "wechat_draft"])

    # Refuse a draft whose article has problems before anyone is asked to approve it:
    # approving would only lead to a failed step and another approval request.
    def hold_problem_drafts(request, next_):
        if request.step.name != "wechat_draft" or config.allow_problems:
            return next_()
        problems = _steps.recorded_problems(ctx.config, request.task)
        if problems:
            return Deny("article has problems: " + "; ".join(problems))
        return next_()

    ctx.on("step/pre-execute", hold_problem_drafts)

    names = [config.appsecret] + ([] if config.appid else [config.appid_credential])
    ctx.doctor.register(lambda: ctx.credentials.check(names, "wechat credentials"))
    ctx.cli.register(_commands(ctx, config))


def _commands(ctx, config: WechatConfig) -> typer.Typer:
    group = typer.Typer(name="wechat", help="WeChat Official Account: preview, credentials.")

    @group.command("token")
    def token() -> None:
        """Check the credentials and this machine's IP whitelisting."""
        step = _steps.WeChatDraftStep(config, ctx.credentials)
        try:
            client = _steps.make_client(*step._account())
            _, expires_in = client.access_token()
        except Exception as exc:  # noqa: BLE001 - shown to the user, value-free
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"credentials work; access token valid for {expires_in}s (not shown)")

    @group.command("preview")
    def preview(
        article: Annotated[Path, typer.Argument(help="The Markdown article.")],
        out: Annotated[
            Path | None, typer.Option("--out", "-o", help="Where to write the HTML.")
        ] = None,
    ) -> None:
        """Lay out an article and write a phone-width preview page."""
        parsed = load_article(article)
        rendered = render(
            parsed.body,
            resolve_image=lambda src: (
                p.as_uri() if (p := _steps.local_image(src, parsed.base_dir)) else src
            ),
            theme=config.theme,
            references_label=config.references_label,
        )
        problems = _steps.article_problems(parsed)
        target = out or parsed.source_path.with_suffix(".wechat-preview.html")
        target.write_text(_steps.preview_page(parsed, rendered.html, problems), encoding="utf-8")
        for problem in problems:
            typer.echo(f"warning: {problem}", err=True)
        typer.echo(str(target))

    return group


plugin = Plugin(
    name="wechat",
    apply=apply,
    Config=WechatConfig,
    inject=["config", "steps", "workflows", "credentials", "doctor", "cli"],
)

BUNDLE = [{"insert": [{"id": "wechat", "plugin": "wechat"}]}]
