# Contributing

Thanks for helping improve Fusion Free Deburr. You don't need to be a
programmer. Ideas, bug reports and test results from real machining are as
useful as code.

## Found a bug or have an idea?

1. Look at [what already exists](docs/features.md). The feature may already be
   there.
2. Search the [issues](https://github.com/jkkicks/Fusion-Free-Deburr/issues)
   and [pull requests](https://github.com/jkkicks/Fusion-Free-Deburr/pulls),
   including closed ones. If someone already asked, add a comment there.
3. Otherwise [open an issue](https://github.com/jkkicks/Fusion-Free-Deburr/issues/new/choose)
   and pick **Bug report** or **Feature idea**.

Please open an issue before writing code for anything bigger than a small fix,
so we can agree it fits before you spend time on it.

## Using an AI assistant (ChatGPT, Codex, Claude, Copilot, Cursor...)

That's welcome. This repo includes instructions for AI assistants in
[AGENTS.md](AGENTS.md), and most tools read them automatically. To be sure,
start your session with:

> Read AGENTS.md and follow it. I'd like to: *describe what you want*.

The assistant will check whether the feature already exists, help you open an
issue, make the change, run the tests and open a pull request here. You can
ask it to explain any step.

## Making a change

1. **Fork** this repo on GitHub and make a branch, for example
   `feat/side-entry-option`.
2. **Make one focused change.** Add tests for new behavior.
3. **Run the tests:** `python -m unittest discover -s tests`
4. **Try it in Fusion.** Press **Shift+S**, click **+** beside **My Add-Ins**,
   choose the `FusionFreeDeburr` folder in your copy, and run it. Click Stop
   then Run after each change. Simulate the toolpath.
5. **Update the docs:** [README.md](README.md) for anything users see,
   [docs/features.md](docs/features.md) for new or changed features, and
   [docs/technical-notes.md](docs/technical-notes.md) for geometry, safety and
   test results.
6. **Open a pull request to this repo**, not just your fork, and fill in the
   template. Say what you tested in Fusion, and if AI helped, say which tool.

A maintainer will review it, add release labels and merge it. Merged changes
are released automatically.

## Credit

Everyone who contributes gets credit in the pull request and release notes.
Please give the same credit to others: mention whose ideas or code you built
on, and keep the [LICENSE](LICENSE) and its copyright notice in any copy you
share.

This project is MIT licensed, so you may fork and share it. If you share your
own version, say clearly that it is a fork of
[Fusion Free Deburr](https://github.com/jkkicks/Fusion-Free-Deburr) and what
you changed. Contributing your changes here instead means everyone gets them
and you don't have to keep a separate copy up to date.

## Safety

This add-in makes toolpaths for real machines. Never weaken a safety or
clearance check to make a case work. If a check blocks something that should
work, open an issue and explain the case.
