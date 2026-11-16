# Releases and updates

## Release tracks

There are two tracks. Both use the PR labels below for notes and version numbers.

- **Stable** is what most people should install. It is the only track GitHub
  marks **Latest**, so the repository sidebar, the README download links and
  `releases/latest` always point at the newest stable release. A stable release
  only happens when someone runs **Build release** by hand with channel
  `stable` and the maintainer approves it.
- **Nightly** is for testers. Every night **Build release** publishes main as a
  prerelease such as `v0.1.1-nightly.3`, but only if something has merged since
  the last release of either kind. With 20 merges in a day there is still only
  one nightly.

Version numbers always go up across both tracks, so a stable release outranks
the nightlies before it: `0.1.0-nightly.1 < 0.1.0 < 0.1.1-nightly.0 < 0.2.0`.
The publisher refuses any version that is not newer than every published
release. The early `0.1.0-alpha.N` builds are older prereleases on the same
scale.

## How it works

1. A PR merges and CI passes on main. The `Release notes` workflow updates two
   draft releases on the Releases page. It never publishes.
   - The **nightly draft** lists changes since the last release of either kind.
   - The **stable draft** lists everything since the last stable release, so
     it is a preview of the next stable release's notes.
2. `Build release` runs: nightly on a schedule (10:23 UTC), or stable by hand.
   It checks that CI passed on main for the source commit, refreshes the
   matching draft for that exact commit, runs the tests, and builds the ZIP,
   `update.json` and `SHA256SUMS`. It downloads every uploaded asset and
   compares bytes before publishing. A stable release is marked Latest; a
   nightly is a prerelease and never is.

The publisher stamps the packaged manifest without changing the source manifest.
Published releases cannot be overwritten by these workflows. GitHub replaces the
`tag_name` of a draft with an `untagged-...` placeholder when it is retargeted
before its tag exists, so every update resends the real tag name and, after
publishing, the workflow fails unless the release carries that tag and the tag
resolves to the source commit.

Use exactly one category label (`feature`, `bug`, `documentation`, `maintenance`)
and exactly one version label (`release:major`, `release:minor`, `release:patch`)
on each PR. Release Drafter keeps two configs, `.github/release-drafter.yml`
(nightly) and `.github/release-drafter-stable.yml` (stable); everything from
`categories:` down must stay identical in both, and a test checks it.

- **Stable version:** the highest label impact since the last stable release
  (`0.1.0` plus a `release:minor` PR gives `0.2.0`). The first stable release is
  `0.1.0`, because there is no earlier one.
- **Nightly version:** the first nightly after a stable release takes its base
  from the labels at that point (`0.1.1-nightly.0` or `0.2.0-nightly.0`), and
  later nightlies only advance the counter. The base stays at or below the next
  stable version, so ordering holds even when a bigger change merges later.

Direct commits to main also go out after CI, but PR labels give better notes.

## Cutting a stable release

1. Merge the work and wait for CI on main.
2. Optional: read the stable draft on the Releases page to check the notes.
3. **Actions > Build release > Run workflow**, on `main`, channel `stable`.
   - **source_sha**: leave empty for the latest main, or give the commit you
     tested in Fusion.
   - **version**: leave empty to use the labels, or set it, for example
     `1.0.0`. It must be newer than every published release.
4. Approve the pending `release` deployment when GitHub asks.

To get a test build without waiting for the night, run it with channel
`nightly`. A commit that is already a stable release cannot be released as
stable again. Do not publish a draft by hand: the workflow must attach and
verify the assets first.

## GitHub setup

1. Create the seven labels above in repository settings.
2. Require the CI `labels` and both `test` jobs for main-branch PRs. Protect main
   against direct pushes if every release must have labeled PR notes.
3. Allow Actions and the pinned external actions in this repository. The built-in
   `GITHUB_TOKEN` needs contents-write permission for the release workflows;
   no personal access token or token on users' machines is needed.
4. Enable immutable releases if available.
5. The `release` environment gates stable releases: it requires approval from
   the maintainer and only allows `main`. Nightly runs use an unprotected
   `nightly` environment, which GitHub creates on first use.
6. GitHub turns off scheduled workflows in a public repository after 60 days
   without activity. If nightlies stop, re-enable `Build release` in Actions.

A retry accepts identical existing draft assets, rejects mismatches and extra
assets, and refuses any already published version. To change bytes, create a
new version. Review/remove mismatched draft assets before retrying a failed build.

## In-app updates

The add-in checks for updates itself (`FusionFreeDeburr/updater.py` for the
logic, `updater_fusion.py` for Fusion, `resources/updater/index.html` for the
window). Settings live outside the add-in folder, in
`%LOCALAPPDATA%\FusionFreeDeburr\updater` on Windows and
`~/Library/Application Support/FusionFreeDeburr/updater` on macOS.

- **Modes:** *Install automatically*, *Ask me first* (default) and *Don't check
  automatically*. All are in the update window, which opens when an update is
  found or from **Utilities > Add-Ins > Check for Deburr Updates**. Automatic
  checks run about 20 seconds after start, at most once every 20 hours.
- **Channels:** stable reads
  `https://github.com/jkkicks/Fusion-Free-Deburr/releases/latest/download/update.json`,
  so nightlies never reach stable users. Nightly lists releases through the
  GitHub API (paginated, skipping drafts and tags that are not `v`-prefixed
  SemVer) and takes the highest version across both tracks. The channel is set
  from the first installed build (nightly only if that was a `-nightly.N`
  build, otherwise stable) and afterwards only by the user.
- **Versions** compare by SemVer precedence; `version_key()` in `updater.py`
  must agree with `tools/release.py`, and a test checks it.
- **Verification:** HTTPS from GitHub hosts only, `update.json` must name this
  add-in ID and an asset under this repository's release download path, and the
  ZIP must match its size and SHA-256. The ZIP is refused if it has unsafe
  paths, links, an unexpected layout, or a manifest with another ID or version.
- **Installing:** the verified add-in folder is staged outside the add-in. Its
  files replace the add-in's files when the add-in stops: when Fusion closes,
  or on Stop in Scripts and Add-Ins. A staged update older than or equal to
  what is in the folder (for example after a manual install) is discarded,
  never installed. Each file is written, flushed and renamed into place. If any
  fails, the old files are put back and the update is retried at the next
  stop, up to three times, and the error shows in the update window. Only files
  a previous update installed are ever deleted.
- **Interrupted installs:** an install journal in `state.json` records what is
  being copied. If Fusion is killed part-way, the next start finishes the
  install before importing any toolpath module, and keeps Deburr off for that
  session, so old and new modules never run together. If the folder was
  changed by hand since, the journal is dropped instead of overwriting it. If
  the install still can't be finished after three starts, Deburr stays off
  until the add-in is reinstalled by hand.
- **Rollback:** the replaced files are kept. If the new version fails to start
  (including failing to import), the user is asked whether to restore the
  previous version, which is then installed at the next stop and the failed
  version is skipped.
- **Not updated automatically:** a git checkout (a `.git` folder in or beside
  the add-in folder), a read-only folder, and releases whose `update.json` says
  `installation_mode: manual`, needs a newer `minimum_loader_version` than
  `UPDATER_VERSION`, or doesn't list the user's OS. These get the window with a
  link to the release page instead, except git checkouts, which only log it
  (the window still explains it on a manual check).

`tools/release.py` writes `installation_mode: automatic` and
`minimum_loader_version: 1`. Raise `minimum_loader_version` together with
`UPDATER_VERSION` if a release ever can't be installed by older updaters.
Releases up to v0.1.1 have no updater and say `manual`, so users install the
first updater-enabled release by hand once.

Not covered: `update.json` is not signed, so automatic installs trust GitHub
and this repository's release process (the same trust as downloading a release
by hand). A broken `updater.py`, `updater_fusion.py` or entry point can't roll
itself back; the user then reinstalls by hand. Fusion testing is still needed
for: install on Fusion exit, install on Stop/Run, the rollback prompt
(including whether Fusion calls `stop()` after a failed `run()`), whether
Fusion keeps add-in files open during `stop()` on Windows, macOS (certificate
handling in Fusion's Python) and offline use. Checks only run when the add-in
starts, so a Fusion session left open for days is not re-checked.

References: [Release Drafter v6.1.0](https://github.com/release-drafter/release-drafter/tree/v6.1.0),
[GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
