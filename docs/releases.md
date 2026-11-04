# Releases and updates

## Release policy

Merges to `main` run CI on Windows and Linux. Successful main-branch push CI
triggers Release Drafter and publication in the same orchestration workflow.
Release Drafter proposes numbered alpha releases while the app is in preview.
The publisher stamps the packaged manifest without changing the source manifest,
builds the canonical installation ZIP, and uploads `update.json` and `SHA256SUMS`.
It downloads every uploaded asset and compares bytes before publishing the draft.
Published releases cannot be overwritten by this workflow.

Use exactly one category label (`feature`, `bug`, `documentation`, `maintenance`)
and exactly one version label (`release:major`, `release:minor`, `release:patch`)
on each PR. Release Drafter records the highest version impact. While continuing
an existing alpha series, its prerelease mode advances the alpha counter on the
same version base (for example, `0.1.0-alpha` to `0.1.0-alpha.1`). When starting
a prerelease from a stable version, the highest impact selects the version base.
Direct commits to main also release after CI, but PR labels provide better notes.
The publisher skips a source commit that already has a published release, so
rerunning main CI does not create another release of identical source.

## GitHub setup

1. Create the seven labels above in repository settings.
2. Require the CI `labels` and both `test` jobs for main-branch PRs. Protect main
   against direct pushes if every release must have labeled PR notes.
3. Allow Actions and the pinned external actions in this repository. The built-in
   `GITHUB_TOKEN` needs contents-write permission for the release workflows;
   no personal access token or token on users' machines is needed.
4. Enable immutable releases if available. Do not manually publish Release
   Drafter's draft: the pipeline must attach and verify the assets first.
5. For manual recovery, create a `release` environment. Add required reviewers
   only if manual recovery should require approval. Automatic publication does
   not use this environment.

The `Release notes` workflow can be dispatched to prepare a draft without
publishing it. `Build release` accepts the draft version and exact full main
commit SHA, then rebuilds and verifies assets. Publication defaults off there.
A retry accepts identical existing draft assets, rejects mismatches and extra
assets, and refuses any already published version. To change bytes, create a
new version. Review/remove mismatched draft assets before retrying a failed build.

## Update contract and remaining implementation

The ZIP is the single installation payload. `update.json` schema 1 records the
app ID, version, channel, source SHA, Windows support, release notes URL, payload
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
safe archive validation, compatibility checks and failed-start rollback. Preview
discovery must paginate releases and compare SemVer, including numbered alpha
versions. Developer checkouts must stay notification-only. Network work must run
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
