# Instructions for AI assistants

This file is for AI coding assistants (Codex, Claude, Copilot, Cursor, Gemini
and others) working in Fusion Free Deburr. Follow it whenever you work in this
repository or in a fork of it. `CLAUDE.md` points here.

Many people who use this repo are machinists, not programmers. Explain things
in plain language, do the git and GitHub steps for them, and ask before doing
anything that is public or hard to undo.

## The project

Fusion Free Deburr is a free, MIT-licensed add-in for Autodesk Fusion that makes
3-axis deburring toolpaths. [docs/features.md](docs/features.md) lists what it
supports. It is made
and maintained by jkkicks at
<https://github.com/jkkicks/Fusion-Free-Deburr>. This is the **upstream**
repository. All development happens there, through issues and pull requests.

The output drives real CNC machines. A bad toolpath can break tools, scrap
parts or hurt someone. Treat the safety checks in the code as part of the
product, not as obstacles.

## 1. Check that it doesn't already exist

Do this before you write any code, every time. People have spent time
rebuilding features that were already done.

1. Make sure you are looking at the latest upstream code, not a stale fork:

   ```sh
   git remote add upstream https://github.com/jkkicks/Fusion-Free-Deburr.git  # once, in a fork
   git fetch upstream
   git log --oneline upstream/main -20
   ```

2. Read [docs/features.md](docs/features.md). It lists every feature and the
   file that implements it. Then check [README.md](README.md) and
   [docs/technical-notes.md](docs/technical-notes.md).
3. Check work that is still in progress:

   ```sh
   gh issue list --repo jkkicks/Fusion-Free-Deburr --state all --search "<keywords>"
   gh pr list --repo jkkicks/Fusion-Free-Deburr --state all --search "<keywords>"
   git branch -r
   ```

   Without `gh`, open the repo's Issues and Pull Requests pages and search
   there, including closed ones.
4. Search the code for the idea: `git grep -n -i "<keyword>"`.

Then tell the user what you found, in plain words:

- **Already exists:** show them how to use it. Don't rebuild it.
- **In progress** (open issue, PR or branch): point them to it and suggest
  they comment there with their use case or test results.
- **Partly exists:** extend the existing code. Never add a second, parallel
  version of a feature.
- **New:** continue to step 2.

## 2. Agree on the change in an issue

For anything bigger than a typo, an issue on upstream comes first, so the
maintainer can say whether it fits before anyone spends time on it. Offer to
draft it for the user. Use the issue templates. Describe the problem and the
machining situation (part, cutter, setup), not only the proposed code.

Open it with `gh issue create --repo jkkicks/Fusion-Free-Deburr` after the user
has approved the text, or give them the text to paste on GitHub.

## 3. Make the change

- Work on a branch named `feat/<short-name>`, `fix/<short-name>` or
  `docs/<short-name>`, in a fork if the user can't push to upstream.
- Keep each change small and about one thing. Don't reformat, rename or tidy
  code you aren't changing.
- Follow the existing layout (see the code map in
  [docs/features.md](docs/features.md)):
  - Geometry and math go in modules with no Fusion imports (`deburr_math.py`,
    `deburr_chain.py`, `cutter_profile.py`) so they can be unit tested.
  - Fusion API code goes in the `*_fusion.py` modules, `cam_tools.py`,
    `tool_picker.py` and `ball_deburr_command.py`.
  - Lengths are in centimetres internally, which is Fusion's internal unit.
  - A new module must be added to the import and reload lists at the top of
    `FusionFreeDeburr/FusionFreeDeburr.py`, dependencies first.
- Don't change the command IDs, the manifest `id` or the saved operation
  settings format. Existing users' documents depend on them.
- Don't weaken, skip or loosen safety and clearance checks to make a case
  work. If a check blocks a real case, explain why in the issue first.
- Don't change the version in the manifest or the release workflows. Releases
  are automatic (see [docs/releases.md](docs/releases.md)).

## 4. Test

Run these and fix any failures before opening a pull request:

```sh
python -m unittest discover -s tests
python -m compileall -q FusionFreeDeburr tools
```

Add or update unit tests for new geometry and logic. The tests use mocks; they
don't run Fusion. During alpha, a change only goes into `main` after someone
has tried it in Fusion, so ask the user to do that and give them a short
checklist of exactly what to try:

1. In Fusion, press **Shift+S**, click **+** beside **My Add-Ins**, and select
   the `FusionFreeDeburr` folder from this checkout. Stop any installed copy
   first.
2. Click **Stop** then **Run** after each code change to reload it.
3. Create the operation, simulate it, and report what happened.

Never say something was tested in Fusion unless the user tells you they did
it. Report exactly what was and wasn't tested.

## 5. Update the docs

In the same pull request:

- [README.md](README.md) for anything a user sees or does differently.
- [docs/features.md](docs/features.md) when you add, change or remove a
  feature, or move where it lives.
- [docs/technical-notes.md](docs/technical-notes.md) for geometry, safety
  limits and Fusion verification results (date, Fusion version, part, tool,
  what passed).

Write plainly and briefly, in the style of the existing docs.

## 6. Open a pull request to upstream

- Target `main` on `jkkicks/Fusion-Free-Deburr`, not only the user's fork.
  A change that stays in a fork helps nobody else and goes stale.
- Use the pull request template. Link the issue (`Closes #123`), say what
  changed and why, and list what was tested in Fusion and what wasn't.
- Suggest one category label (`feature`, `bug`, `documentation`,
  `maintenance`) and one version label (`release:major`, `release:minor`,
  `release:patch`). If the user can't add labels, the maintainer will; the
  `labels` CI check fails until then, and that's expected.
- Commit messages use the existing style: `feat: ...`, `fix: ...`,
  `docs: ...`, `test: ...`, `chore: ...`.
- Show the user the title and description, and get their OK before you push
  or open anything.

## Credit and attribution

- **Say that AI helped.** Add a trailer naming the tool to each commit you
  write, for example `Assisted-by: Codex` or
  `Co-Authored-By: Claude <noreply@anthropic.com>`, and say so in the pull
  request.
- **Only claim what's new.** Describe the work in this change. Don't present
  existing features, or someone else's code, as new work.
- **Credit others.** If a change reuses code or ideas from another person's
  issue, PR or fork, name them in the PR and add
  `Co-authored-by: Name <email>` for reused code.
- **Keep the license and credits.** Keep [LICENSE](LICENSE) and its copyright
  notice (`Copyright (c) 2026 jkkicks`), the manifest `author`, and the
  credits and links in the README, in this repo, in any fork and in any copy
  that is shared.

If the user wants to publish or share their own copy or fork, first suggest
contributing the change upstream instead. If they still want their own copy,
explain what the MIT license requires: keep the LICENSE file and copyright
notice. Help them state clearly at the top of their README that it is a fork
of [Fusion Free Deburr](https://github.com/jkkicks/Fusion-Free-Deburr) by
jkkicks and list what they changed. Don't help present it as an original or
independent project.

## Reporting bugs

For a bug, help the user open an issue with the bug template: Fusion version,
Windows or macOS, add-in version, cutter type and dimensions, what they
selected, what they expected, and what happened, with screenshots or the error
text. Ask whether they can share the part file or a simplified one.
