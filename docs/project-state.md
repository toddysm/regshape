# Project state

Captured on **2026-10-30** against
[main at ed17d2e](https://github.com/toddysm/regshape/commit/ed17d2eef64da73528998e5c0917dff4e91820c5).
This inventory distinguishes implemented behavior from partial capabilities,
unmerged proposals, and planned work. It is a dated snapshot, not an assertion
that every registry or platform has been tested.

Tracking is **issues-only** because Project access is unavailable in this
workspace. The linked issues and pull requests remain authoritative for newer
changes. The approved bootstrap scope includes the baseline inventory and the
Planned live-progress row; its design remains in the feature issue, not a
separate design document.

## Status meanings

- **Implemented:** the described capability is present on the captured main.
- **Partial:** useful behavior exists, but the stated gaps remain.
- **In progress:** work is proposed in an open pull request, not yet on main.
- **Planned:** tracked work that has not been implemented.

The **PRD doc** column can link either a product requirements document or an
issue-backed design. Documentation links are classified by purpose. A dash
means no corresponding artifact or verified landing date is recorded here.
Landing dates are UTC dates for the cited addition or extension, not necessarily
the first version of every operation in that row.

## Capability inventory

| Capability | State | Tracking | PRD doc | Docs (arch/dev) | Docs (user) | Landed |
| --- | --- | --- | --- | --- | --- | --- |
| Registry authentication and Docker credential resolution | Implemented | - | - | - | [Command reference: authentication](../specs/cli/auth.md) | - |
| Manifest retrieval, metadata, push, and deletion | Implemented | - | - | [API reference: manifests](../specs/operations/manifests.md) | [Command reference: manifests](../specs/cli/manifest.md) | - |
| Blob retrieval/verification, monolithic and chunked upload, mounting, and deletion | Implemented | - | - | [API reference: blobs](../specs/operations/blobs.md) | [Command reference: blobs](../specs/cli/blob.md) | - |
| Registry ping and catalog listing, including catalog pagination | Implemented | - | - | [API reference: catalog](../specs/operations/catalog.md) | [Command references: ping](../specs/cli/ping.md), [catalog](../specs/cli/catalog.md) | - |
| Tag listing/deletion and automatic pagination | Implemented | [#69](https://github.com/toddysm/regshape/issues/69), [merged #70](https://github.com/toddysm/regshape/pull/70) | [Issue specification: #69](https://github.com/toddysm/regshape/issues/69) | [API reference: tags](../specs/operations/tags.md) | [Command reference: tags](../specs/cli/tag.md) | 2026-10-30 |
| Referrer listing/filtering/pagination, including tag-to-digest resolution | Implemented | [#46](https://github.com/toddysm/regshape/issues/46), [merged #68](https://github.com/toddysm/regshape/pull/68) | - | [API reference: referrers](../specs/operations/referrers.md) | [Command reference: referrers](../specs/cli/referrer.md) | 2026-10-30 |
| Local Open Container Initiative (OCI) image-layout authoring, validation, and registry push | Implemented | - | - | [Design: layout creation](../specs/layout/oci-layout-create.md), [design/reference: layout push](../specs/cli/layout-push.md) | [How-to: create layouts](guides/create-oci-layout.md), [command reference: layouts](../specs/cli/layout.md) | - |
| Docker daemon image listing, export/conversion, and registry push | Implemented | - | - | [Design: Docker integration](../specs/docker/docker-desktop-integration.md) | [How-to: Docker integration](guides/docker-desktop-integration.md) | - |
| Opt-in request diagnostics, timing, metrics, and log output | Partial | Ordinary-transfer coverage gaps are not separately filed | - | [Design: telemetry](../specs/telemetry-enhancements.md) | [Option reference: current telemetry flags](#telemetry-options-current-baseline) | - |
| Break Mode request mutation | Planned | [#29](https://github.com/toddysm/regshape/issues/29) | [Issue requirements: #29](https://github.com/toddysm/regshape/issues/29) | [Architecture: Break Mode](../specs/architecture.md) | - | - |
| Break Mode telemetry integration | Planned | [#38](https://github.com/toddysm/regshape/issues/38); depends on #29 | [Issue requirements: #38](https://github.com/toddysm/regshape/issues/38) | [Design: telemetry enhancements](../specs/telemetry-enhancements.md) | - | - |
| Python Package Index (PyPI) release publication | Implemented | [Closed #57](https://github.com/toddysm/regshape/issues/57), [v0.1.1 release](https://github.com/toddysm/regshape/releases/tag/v0.1.1) | - | [Developer guide: contributing](../CONTRIBUTING.md) | [Overview/install reference](../README.md) | - |
| Homebrew distribution and release-driven formula updates | Partial | [Tracker #63](https://github.com/toddysm/regshape/issues/63), [remaining documentation fix #62](https://github.com/toddysm/regshape/issues/62) | - | - | [How-to: Homebrew installation](guides/homebrew-install.md) | - |
| OCI image-specification guidance for the development skill | In progress | [#66](https://github.com/toddysm/regshape/issues/66), [open #67](https://github.com/toddysm/regshape/pull/67) | [Issue requirements: #66](https://github.com/toddysm/regshape/issues/66) | [Current development skill](../.github/skills/regshape-dev/SKILL.md) | - | - |
| Live transfer progress bars across blob, layout, and Docker commands | Planned | [Feature #72](https://github.com/toddysm/regshape/issues/72), [implementation #73](https://github.com/toddysm/regshape/issues/73), [#74](https://github.com/toddysm/regshape/issues/74), [#75](https://github.com/toddysm/regshape/issues/75), [#76](https://github.com/toddysm/regshape/issues/76), [documentation #77](https://github.com/toddysm/regshape/issues/77) | [Lightweight design: #72, revision 1](https://github.com/toddysm/regshape/issues/72) | - | - | - |

## Evidence and important boundaries

### Registry and image operations

The command implementations live in [the command package](../src/regshape/cli/).
Their library counterparts are in [authentication](../src/regshape/libs/auth/),
[manifests](../src/regshape/libs/manifests/),
[blobs](../src/regshape/libs/blobs/),
[catalog](../src/regshape/libs/catalog/),
[tags](../src/regshape/libs/tags/),
[referrers](../src/regshape/libs/referrers/),
[layouts](../src/regshape/libs/layout/), and
[Docker integration](../src/regshape/libs/docker/).
Docker push exports/converts a daemon image to a local layout before uploading;
this is not a general registry-to-local full-image pull command.

Automatic tag pagination is opt-in. It includes a client safety limit of ten
consecutive empty linked pages; a terminal empty page completes normally, and
received tags reset the counter. This is a client policy, not an OCI-mandated
page limit. Referrer tag resolution and tag pagination are both merged.

### Telemetry and progress are not complete coverage

[Telemetry configuration](../src/regshape/libs/decorators/__init__.py) defaults
to disabled and emits local output/logs when enabled. It is not an automatic
remote monitoring exporter. Source inspection and bounded mocked probes found:

- [Docker push](../src/regshape/cli/docker.py) does not expose the common
  telemetry options or a whole-command timing wrapper.
- [Token exchanges](../src/regshape/libs/auth/registryauth.py) bypass the
  instrumented registry request wrapper.
- [Request telemetry](../src/regshape/libs/decorators/call_details.py) records
  responses, not pre-response transport failures. Streaming byte metrics rely
  on response length metadata; per-request timing ends before subsequent body
  consumption. Outer method/workflow timers can still measure that consumption.
- [Nested scenario timing](../src/regshape/libs/decorators/scenario.py) resets
  shared counters, so an outer push summary can omit earlier nested requests.

Issue [#38](https://github.com/toddysm/regshape/issues/38) is specifically about
Break Mode logging, not these ordinary-transfer gaps. No additional telemetry
issues were created as part of this bootstrap.

#### Telemetry options (current baseline)

Use these leaf-command options only where the command's `--help` lists them.
They are not currently exposed by `docker push`. Collection is opt-in: format,
verbosity, and log-file options alone do not enable it.

| Option | Placement | Default | Behavior |
| --- | --- | --- | --- |
| `--time-methods` | After the leaf command | Off | Collect timing for instrumented methods. |
| `--time-scenarios` | After the leaf command | Off | Render timing summaries for instrumented workflows. |
| `--debug-calls` | After the leaf command | Off | Trace instrumented request/response headers and elapsed time. |
| `--metrics` | After the leaf command | Off | Show aggregate request, byte, status, and timing metrics, subject to the limitations above. |
| `--telemetry-format` | After the leaf command | `text` | Select `text` or `json` telemetry events; JSON events are newline-delimited. This does not change ordinary command output. |
| `--telemetry-verbosity`, `-tv` | After the leaf command | `1` | Select `0` (off), `1` (normal), or `2` (verbose). Zero overrides the telemetry enable flags. |
| `--log-file PATH` | Before the command group | None | Append enabled telemetry output to a file as well as standard error. It does not enable telemetry by itself. |

For example, keep the tag result on standard output and mirror enabled
telemetry events to a log file:

```bash
regshape --log-file telemetry.log tag list \
  -i registry.example.com/team/image --json \
  --time-methods --time-scenarios --metrics --debug-calls \
  --telemetry-format json
```

#### Progress baseline

The current [layout push renderer](../src/regshape/cli/layout.py) opens a
per-blob bar only for interactive, non-JSON command output and completes it when
the blob finishes. The [layout library](../src/regshape/libs/layout/operations.py)
has start/done callbacks but no live byte-progress events. Other transfer
commands do not currently have progress bars.

Feature [#72](https://github.com/toddysm/regshape/issues/72) addresses that gap.
Its design and breakdown are **unapproved**, its row is **Planned**, and no
implementation has started. The proposed callback names, progress opt-out, and
redraw budget remain subject to design approval. The exactly one final
user-documentation item is [#77](https://github.com/toddysm/regshape/issues/77).

### Release and Homebrew state

The [release workflow](../.github/workflows/publish.yml) implements publication
and release-triggered formula updates. Version 0.1.1 is published. Tap
installation/test/audit validation was repaired in
[toddysm/homebrew-regshape#1](https://github.com/toddysm/homebrew-regshape/pull/1);
the merged change passed on macOS and Linux during that work.

The latest Homebrew tracking checkpoint still marks the complete
release-to-PyPI-to-formula-bump flow as unverified. The guide already exists;
[#62](https://github.com/toddysm/regshape/issues/62) remains open because its
verification example uses unsupported `regshape --version`. It is the only
open child of [#63](https://github.com/toddysm/regshape/issues/63). Older checkbox
text in that tracker should not be read as reopening already closed work.

## Validation baseline and maintenance

The [test workflow](../.github/workflows/tests.yml) covers Python 3.10, 3.11,
and 3.12. [Post-merge main run 40982870365](https://github.com/toddysm/regshape/actions/runs/40982870365)
passed **1,300 tests per version** at the captured revision, with successful
Python/Actions CodeQL analyses. The registry feature tests use mocked responses;
this is not evidence that every live registry has passed end-to-end testing.

Update this inventory when related work lands or a tracking decision changes.
Do not mark a feature Implemented merely because its design or a pull request
exists. For the new lightweight feature, approval of the issue design and
breakdown, authorized merging of this inventory bootstrap, and a reviewed
transition to In progress are required before Build. Completion also requires
the implementation/documentation items and an inventory close-out update.
