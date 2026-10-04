# Example: creator-publishing-pipeline

An advanced content pipeline for creators, operators, and personal-brand
builders. From raw materials (notes + a draft/transcript + assets) it assembles
a **multi-platform draft package** and a publish checklist.

By design it is **draft-only** — it never publishes anything. That keeps you in
control and avoids platform, account-safety, and compliance risk. Publishing
stays a deliberate, manual step.

Workflow:

```
collect_materials -> extract_highlights -> draft_article -> render_platform_package -> create_publish_checklist -> archive
```

(The first five steps come from the
[runspool-example-creator](../../plugins/runspool-example-creator) plugin package,
which also contributes the workflow; `archive` is built in. Install it with
`uv sync` from the repository root, or `pip install runspool-example-creator`.)

## Run it

From this directory:

```bash
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./materials --workflow creator_publishing --name "Local-first automation"
runspool -c config.yaml run
runspool -c config.yaml inspect 1
```

The package lands in `workspace/ready/1/dist/`:

```
dist/
  article.md
  wechat.html
  x-thread.md
  linkedin-post.md
  bilibili-description.md
  publish-checklist.md
  manifest.json
  assets/
```

## Future: optional publish adapters

A natural next step is to add **opt-in** adapter steps that submit a *draft*
(never an auto-publish) to a platform's API, for example (the official
[runspool-wechat](../../plugins/runspool-wechat) plugin does this for WeChat, behind
an approval):

```
submit_wechat_draft
submit_wordpress_draft
submit_bilibili_draft
```

These would be additional plugin steps you append to the workflow and enable
explicitly. The pipeline deliberately ships without them.

## Reset

```bash
rm -rf workspace
```
