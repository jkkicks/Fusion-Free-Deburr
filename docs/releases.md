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

## Updater channels

`update.json` declares `channel: stable` or `channel: nightly`. Older `0.1.0-alpha.N`
releases say `preview`; treat them as nightly. The future updater should:

- **Keep the channel as a setting**, stored outside the runtime folders. Set it
  from the channel of the build the user first installed, and let them change
  it. Don't work it out from the running version: a nightly user who updates to
  a stable release must stay on nightly.
- **Stable:** download `https://github.com/jkkicks/Fusion-Free-Deburr/releases/latest/download/update.json`.
  GitHub redirects that URL to the Latest release, which is always stable, so
  nightlies can never reach stable users and no API call is needed.
- **Nightly:** list releases through the GitHub API (paginate, skip drafts and
  tags that are not `v`-prefixed SemVer), take the highest version across both
  tracks, and use its `update.json`. Nightly users then get every nightly, plus
  each stable release, because it outranks the nightlies before it.
- **Compare versions by SemVer precedence**, as `version_key()` in
  `tools/release.py` does. Only offer an update when its version is higher than
  the running one. Checking once a day is enough, because nightlies come out at
  most once a day.

## Update contract and remaining implementation

The ZIP is the single installation payload. `update.json` schema 1 records the
app ID, version, channel, source SHA, supported OSes (from the manifest), release notes URL, payload
URL, byte size and SHA-256. The source manifest remains a development baseline;
the manifest inside each release ZIP contains the release's exact version.

The desired default is automatic download and activation on the next Fusion
start. This branch currently establishes release infrastructure, not an active
in-app updater: metadata explicitly declares `installation_mode: manual` until
the loader is implemented and tested. Existing installations need one manual
installation of that future loader-enabled release.

Updater work is deferred until the GitHub pipeline produces a real release
package. A local prototype is preserved separately from this release branch.
Next integrate a production stable loader, side-by-side runtimes outside Fusion's add-in
discovery folder, settings outside runtime folders, atomic pending/current state,
safe archive validation, compatibility checks and failed-start rollback. Nightly
discovery follows the channel rules above. Developer checkouts must stay notification-only. Network work must run
off Fusion's UI thread; activation happens before importing runtime modules.
Verify metadata signatures before enabling automatic installation. A checksum
alone does not independently establish publisher identity.

Fusion testing is needed for registration, restart activation, offline use and
rollback after failed startup. Hosted CI does not run Fusion or verify CAM output.
During alpha, main should only receive changes that have been tested in Fusion.
The user has smoke-tested the local `0.1.0-test.1` installation ZIP: it activated
and generated a toolpath successfully. The development add-in was restored afterward.
Hosted Actions and publication still need validation on GitHub.

References: [Release Drafter v6.1.0](https://github.com/release-drafter/release-drafter/tree/v6.1.0),
[GitHub workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
