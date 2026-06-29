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

(The first five steps are custom plugins in [steps/creator_steps.py](steps/creator_steps.py);
`archive` is built in.)

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
(never an auto-publish) to a platform's API, for example:

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
