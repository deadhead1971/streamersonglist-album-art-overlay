# Code signing policy

**Status:** code signing for the Windows installer has been **applied for** with
the SignPath Foundation. Until it's approved, installers are unsigned, and
Windows warns about them when you download and run them (see the README). This
page is the policy signed releases will follow, published in advance, as
SignPath asks.

Free code signing provided by [SignPath.io](https://signpath.io), certificate
by [SignPath Foundation](https://signpath.org).

## What is signed

Only the Windows installer, **`AlbumArtOverlay-Setup-<version>.exe`**, attached
to this repository's [GitHub releases](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases).

- It is built from this repository's source by GitHub Actions, on
  GitHub-hosted runners (`.github/workflows/windows-build.yml`, running
  `packaging/build.ps1`), for a version tag. Nothing is built or signed on a
  personal computer.
- The installer contains the app's Python source, and the official
  python.org embeddable Python, whose `python.exe` and `pythonw.exe` are
  already signed by the Python Software Foundation. It also contains
  third-party open-source libraries (listed with their licences in
  `THIRD-PARTY-NOTICES.txt`); their own files are not re-signed.
- Nothing else is signed: not forks, not test builds from branches, and not
  files anyone else builds.

## Team and roles

This project has one maintainer, who holds every role:

| Role | Who | What it means |
|---|---|---|
| Committer and reviewer | [Alan Thompson (@deadhead1971)](https://github.com/deadhead1971), repository owner | the only account with write access to this repository. Changes from anyone else arrive as pull requests, and are reviewed before they're merged |
| Approver | [Alan Thompson (@deadhead1971)](https://github.com/deadhead1971) | approves each release's signing request by hand on SignPath, after checking that the build came from the release tag |

Two-factor authentication is on for the GitHub account, and will be for the
SignPath account.

## Privacy

This program does not send anything to the author, and has no analytics or
telemetry. It contacts StreamerSonglist, artwork search services and GitHub,
for the reasons and with exactly the information described in the
[privacy policy](PRIVACY.md).

## Reporting a problem

If you believe a signed release is malicious or was not built from this
repository, open an issue at
<https://github.com/deadhead1971/streamersonglist-album-art-overlay/issues>, or
report it to SignPath at support@signpath.io.
