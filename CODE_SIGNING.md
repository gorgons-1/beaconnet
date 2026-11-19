# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by
[SignPath Foundation](https://signpath.org/).

**Status:** applied for; until SignPath approves the project, releases are unsigned (Windows shows a SmartScreen warning).

This policy covers **BeaconNet Desktop for Windows** (`BeaconNet.exe`), the only signed
file. It is built from this repository by the GitHub Actions workflow
[`.github/workflows/desktop.yml`](.github/workflows/desktop.yml) and signed only for
tagged releases (`v*`), after a maintainer approves each signing request.

## Team roles

| Role | Who |
|---|---|
| Committers and reviewers | [gorgons-1](https://github.com/gorgons-1) |
| Approvers | [gorgons-1](https://github.com/gorgons-1) |

Every team member uses multi-factor authentication for GitHub and SignPath. Changes from
anyone else arrive as pull requests and are reviewed before they're merged.

## Privacy policy

BeaconNet Desktop doesn't transfer any information to other networked systems unless the
user asks it to. It connects only to the BeaconNet BBS node whose address the user enters,
using SSH. It has no telemetry, analytics, advertising or automatic update checks. Its
settings and the key it creates for "remember this computer" stay in the user's own
profile folder. **Forget this node** deletes them.

The BBS node itself is run by its sysop, who can see what you post and send there. See
[docs/security.md](docs/security.md).
