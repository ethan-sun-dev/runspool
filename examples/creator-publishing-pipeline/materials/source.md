# Why I moved my automation back to my own machine

For years I glued my personal automation together with hosted services. It was
fine until it wasn't: an outage during a deadline, a surprise price change, and
the slow realization that my data was scattered across half a dozen dashboards I
didn't control.

So I moved it back to my own machine. The rule was simple: every job has to be
resumable, observable, and scriptable. If a task dies halfway through, I want to
pick it up exactly where it stopped. If I want to know what happened, the answer
should be one command away. And if I want a script — or an AI agent — to run it,
the tool should speak plain text and JSON, not a proprietary web UI.

That is the whole philosophy. Keep the data local. Make every run inspectable.
Treat the command line as the API. The result is automation I actually trust.
