# runspool-wechat

The official [RunSpool](https://github.com/ethan-sun-dev/runspool) plugin for WeChat
Official Accounts: lay out a Markdown article the way the WeChat editor keeps it,
and save it as a draft through the official API, after a human approves.

It never sends anything to followers. Sending stays a decision made in the WeChat
back end (where you also turn on the original-content declaration, which the API
cannot set).

## Use it

```yaml
# runspool.yaml
workspace_root: ./workspace
bundles: [core, builtin-steps, wechat]
patch:
  - id: wechat
    config:
      author: Your Name
```

```bash
runspool -c runspool.yaml wechat preview post/wechat.md   # phone-width preview page
runspool -c runspool.yaml wechat token                    # check credentials + IP whitelist
runspool -c runspool.yaml add post/wechat.md --workflow wechat_article
runspool -c runspool.yaml run                             # renders, then waits for approval
runspool -c runspool.yaml approve 1                       # approve this draft attempt
runspool -c runspool.yaml run                             # uploads images + cover, saves the draft
```

The article is Markdown with a `# Title`, optional front matter (`digest`, `author`,
`cover`) or a draft template (`## 摘要` section, then `---`, then the body). The
cover is `cover.png` next to the article unless front matter names one.

## Credentials

Credentials are names, resolved by RunSpool's `credentials` service (environment,
`~/.config/runspool/credentials.yaml`, a `.env` next to the profile, `~/.env`):
`WECHAT_APPSECRET`, and `WECHAT_APPID` unless `appid` is set in the plugin config.
This machine's public IP must be on the account's IP whitelist.

## What the layout does

Every style is inline (the editor drops `<style>` and classes); all text is wrapped
in `<span leaf="">` (bare text loses its style when edited again in the back end);
links other than WeChat articles become numbered references at the end (they are not
clickable in an article); raw HTML is escaped; code keeps its line breaks.

This is the first cut of the plugin (RunSpool 0.2): themes, tables and image
compression are coming.
