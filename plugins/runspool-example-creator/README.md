# runspool-example-creator

An example [RunSpool](https://github.com/ethan-sun-dev/runspool) plugin. From raw
materials (notes, a draft or transcript, assets) it builds a **multi-platform draft
package** and a publish checklist. It never publishes anything.

It shows the shape of a plugin package: steps registered with `ctx.steps.register`,
a default workflow contributed with `ctx.workflows.add_default`, and an entry point
in the `runspool.plugins` group so a profile can mount it by name:

```yaml
patch:
  - insert: [{id: creator, plugin: example-creator}]
```

See `examples/creator-publishing-pipeline/` in the RunSpool repository for a
runnable walkthrough.
