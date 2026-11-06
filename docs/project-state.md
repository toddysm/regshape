# Project state

<!-- doc-type: overview -->

Complete artifact delivery reconciled on **2026-11-06 (UTC)** against
[main at 2b2a4d9](https://github.com/toddysm/regshape/commit/2b2a4d9ebb1c32e373da0ba99c96b9f496f02338).
The original baseline was captured on 2026-10-30 at
[ed17d2e](https://github.com/toddysm/regshape/commit/ed17d2eef64da73528998e5c0917dff4e91820c5);
unchanged capability evidence is retained below, while live progress and
artifact-oriented command options, including their required documentation,
are merged.
This inventory distinguishes implemented behavior from partial capabilities,
unmerged proposals, and planned work. It is a dated snapshot, not an assertion
that every registry or platform has been tested.

Artifact-oriented option naming is implemented under
[#87](https://github.com/toddysm/regshape/issues/87). The approved
[functional/engineering specification](../specs/cli/artifact-options.md),
shared support, eight-command integration and final user documentation have
all merged; the four required child issues are closed and unblocked.

Manifest GET progress is **Implemented** under
[#90](https://github.com/toddysm/regshape/issues/90). Its
[approved functional/engineering design](../specs/cli/manifest-progress.md)
and native breakdown are recorded in
[design PR #106](https://github.com/toddysm/regshape/pull/106); runtime
implementation of [library observations #105](https://github.com/toddysm/regshape/issues/105)
is merged through [#108](https://github.com/toddysm/regshape/pull/108).
Command integration [#104](https://github.com/toddysm/regshape/issues/104) is merged
through [#109](https://github.com/toddysm/regshape/pull/109). Final user documentation
[#102](https://github.com/toddysm/regshape/issues/102) is merged through
[#110](https://github.com/toddysm/regshape/pull/110). All four required design,
implementation and documentation children are closed and unblocked. This
inventory close-out records the delivered capability; no release is implied.

Complete artifact download and recursive layout push are **Implemented** under
[#91](https://github.com/toddysm/regshape/issues/91). The
[approved functional/engineering specification](../specs/cli/artifact-pull.md)
covers all-platform tag/digest roots, directory/gzip-tar output, config-inclusive
progress and nested-index push round-trip. Design [#120](https://github.com/toddysm/regshape/pull/120)
is merged. Raw receive, graph, archive, recursive push and command/progress
delivery are merged through [#122](https://github.com/toddysm/regshape/pull/122),
[#123](https://github.com/toddysm/regshape/pull/123),
[#124](https://github.com/toddysm/regshape/pull/124),
[#125](https://github.com/toddysm/regshape/pull/125), and
[#126](https://github.com/toddysm/regshape/pull/126). The final
[user workflow](guides/artifact-pull.md), [command reference](../specs/cli/artifact.md),
and [library reference](../specs/operations/artifacts.md) merged through
[final documentation #127](https://github.com/toddysm/regshape/pull/127),
closing [#119](https://github.com/toddysm/regshape/issues/119). All seven required
design, implementation and documentation children are closed and unblocked.
This inventory close-out records completion, not release publication.
Native operations require Linux/macOS with
compatible filesystems and a trusted private namespace; Windows fails closed.
Config/layer-position downloads are separately
**Planned** under [#112](https://github.com/toddysm/regshape/issues/112), not a
required child of #91. Both target the open 0.2 milestone.

Tracking is **issues-only** because Project access is unavailable in this
workspace. The linked issues and pull requests remain authoritative for newer
changes. Blob/image live-progress design revision 2 remains in its feature issue, not a
separate design document. All five required implementation/documentation items
are closed and no longer dependency-blocked.

**Implemented does not mean released.** Live progress, artifact options and complete-artifact operations target the open
[0.2 milestone](https://github.com/toddysm/regshape/milestone/1); this work did
not publish a release or change the package version.

## Status meanings

- **Implemented:** the described capability is present on the captured main.
- **Partial:** useful behavior exists, but the stated gaps remain.
- **In progress:** implementation is authorized or underway, but the capability
  is not yet complete on main.
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
| Live progress for blob get/upload and layout/Docker push | Implemented | [Feature #72](https://github.com/toddysm/regshape/issues/72), merged [#80](https://github.com/toddysm/regshape/pull/80), [#81](https://github.com/toddysm/regshape/pull/81), [#82](https://github.com/toddysm/regshape/pull/82), [#83](https://github.com/toddysm/regshape/pull/83), [#84](https://github.com/toddysm/regshape/pull/84) | [Lightweight design: #72, revision 2](https://github.com/toddysm/regshape/issues/72) | [Developer reference: transfer callbacks](library/transfer-progress.md), [API reference: blobs](../specs/operations/blobs.md) | [How-to: live transfer progress](guides/live-transfer-progress.md) | 2026-11-03 |
| Artifact-oriented CLI options with compatible legacy aliases | Implemented | [Feature #87](https://github.com/toddysm/regshape/issues/87), merged [#96](https://github.com/toddysm/regshape/pull/96), [#97](https://github.com/toddysm/regshape/pull/97), [#98](https://github.com/toddysm/regshape/pull/98), [#99](https://github.com/toddysm/regshape/pull/99) | [Functional requirements: artifact options](../specs/cli/artifact-options.md#part-i-functional-requirements) | [Engineering design: artifact options](../specs/cli/artifact-options.md#part-ii-engineering-design), [API source: reference parsers](../src/regshape/libs/refs.py) | [How-to: artifact options and migration](guides/artifact-options.md) | 2026-11-04 |
| Live progress for manifest/index GET | Implemented | [Feature #90](https://github.com/toddysm/regshape/issues/90), merged [library #108](https://github.com/toddysm/regshape/pull/108), [command #109](https://github.com/toddysm/regshape/pull/109) and [final docs #110](https://github.com/toddysm/regshape/pull/110) | [Approved functional requirements](../specs/cli/manifest-progress.md#part-i-functional-requirements) | [Approved engineering design](../specs/cli/manifest-progress.md#part-ii-engineering-design), [Callback reference](library/transfer-progress.md#manifest-receive-observations), [API reference](../specs/operations/manifests.md#get_manifest) | [How-to: manifest/index download](guides/live-transfer-progress.md#download-a-manifest-or-index), [Command reference](../specs/cli/manifest.md#manifest-get) | 2026-11-05 |
| Complete artifact download, compressed OCI layout and recursive index push | Implemented | [Feature #91](https://github.com/toddysm/regshape/issues/91), [runtime merged through #126](https://github.com/toddysm/regshape/pull/126), [final docs #127](https://github.com/toddysm/regshape/pull/127) | [Approved functional requirements](../specs/cli/artifact-pull.md#part-i-functional-requirements) | [Approved engineering design](../specs/cli/artifact-pull.md#part-ii-engineering-design), [API reference](../specs/operations/artifacts.md) | [How-to: complete-artifact workflow](guides/artifact-pull.md), [command reference](../specs/cli/artifact.md) | 2026-11-06 |
| Config and zero-based layer-content selection from artifact manifests | Planned | [Feature #112](https://github.com/toddysm/regshape/issues/112) | [Captured requirements](https://github.com/toddysm/regshape/issues/112) | - | - | - |

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
this is not a general registry-to-local full-image pull command. Its existing
multi-manifest tag-override restriction remains: select a platform for those
exports. Live progress did not add multi-platform index publication.

Automatic tag pagination is opt-in. It includes a client safety limit of ten
consecutive empty linked pages; a terminal empty page completes normally, and
received tags reset the counter. This is a client policy, not an OCI-mandated
page limit. Referrer tag resolution and tag pagination are both merged.

### Telemetry coverage remains partial

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
  -a registry.example.com/team/image --json \
  --time-methods --time-scenarios --metrics --debug-calls \
  --telemetry-format json
```

### Live transfer progress

For blob/image transfers, the shared [renderer](../src/regshape/cli/progress.py) serves `blob get`,
both modes of `blob upload`, `layout push`, and `docker push`. It retains one
row per unique blob and overwrites that row for byte updates, retry,
verification/commit, and final state. Image pushes show indeterminate
preparation and unique done/skipped counts; shared references do not duplicate
rows or downgrade an uploaded blob.

The [library callback contract](library/transfer-progress.md) reports actual
logical bytes with trusted or unknown totals. File-backed uploads and integrity
checks use bounded reads; replayable monolithic uploads reset counts on retry.
Both streamed upload protocols require a final PUT with `201 Created` and
applicable digest checks before reporting completion.

`--no-progress`, explicit layout/Docker `--json`, structured telemetry, and
noninteractive stderr suppress animation without changing transfer protocols
or validation. Blob result JSON on stdout alone does not disable stderr progress.
Existing result schemas and
legacy layout callbacks remain compatible. These progress observations do not
repair the separate telemetry gaps above.

Feature [#72](https://github.com/toddysm/regshape/issues/72) is implemented
through [#73](https://github.com/toddysm/regshape/issues/73),
[#74](https://github.com/toddysm/regshape/issues/74),
[#75](https://github.com/toddysm/regshape/issues/75),
[#76](https://github.com/toddysm/regshape/issues/76), and the required final
documentation item [#77](https://github.com/toddysm/regshape/issues/77).
The [user guide](guides/live-transfer-progress.md) contains single-row snapshots,
output-channel rules, retry/error semantics, and supported command examples.

### Manifest/index GET progress

`manifest get` uses the same renderer for one persistent logical manifest/index
row, including byte progress, unknown totals, processing, writing, failure and
cancellation. Final `Downloaded` follows required output and in-context diagnostics,
not merely body receipt. Progress uses interactive stderr; `--no-progress`,
noninteractive stderr and structured telemetry suppress animation. Ordinary
manifest JSON on stdout does not suppress it. The row is temporarily suspended
around stdout output and restored below the result without erasing partial output.

The optional [library observations](library/transfer-progress.md#manifest-receive-observations)
preserve the existing decoded-text tuple and identify each invocation without
requiring a known digest. Observed requests consume actual bytes, close owned
responses, preserve primary failures, and bypass optional response caching.
Buffered calls without an observer retain cache reuse. Request debug previews
remain absent and request timing ends at headers; operation timing includes
consumption. This does not add digest verification, partial-body resume,
credential refresh, or progress-only network probes.

The [manifest workflow](guides/live-transfer-progress.md#download-a-manifest-or-index)
documents tag/digest, raw, part and file variants, exact row snapshots and
recovery. Raw output is decoded text, not byte-exact export. Fetching a manifest
or index does not recursively download its referenced content; full-artifact
download is separately implemented under [#91](https://github.com/toddysm/regshape/issues/91).

### Complete artifact downloads and round trips

`artifact pull` resolves a tag once or verifies a digest root, preserves original
manifest/index bytes, and downloads the complete all-platform graph including
explicit subjects, configuration and layer/content blobs. Shared addresses are
stored once; reverse referrer discovery and arbitrary external descriptor-URL
fetches are not performed. The default output is an OCI directory; explicit
`oci-tar-gzip` output packages the same layout without extracting payloads.

The shared display includes every unique index, manifest and config/layer blob.
Local packaging/publication is separate from network progress. Suppression and
structured-output rules preserve stdout/file data. `layout validate --recursive`
and `layout push` use verified directory/archive snapshots; publication sends
dependencies and subjects before parents, with root aliases last. These are not
resume, parallel transfer or registry transaction/rollback features.

Safe native operations require supported Linux/macOS filesystems and the
documented trusted-private namespace. Windows fails closed. Completed publication
does not promise power-loss durability; partial remote uploads are not deleted
on failure. See the [complete workflow and recovery guidance](guides/artifact-pull.md)
and [API contracts](../specs/operations/artifacts.md) for precise limits,
input/profile requirements, trust boundaries and compatibility notes.

### Artifact-oriented options and migration

The eight manifest/tag/referrer selectors now prefer `--artifact / -a`.
Legacy `--image-ref / -i` remains functional but is deprecated in 0.2
help/documentation, with no runtime warning or scheduled removal date.
Identical repeated targets execute once; different raw values fail with a
generic usage error before credentials or registry requests.

The shared [option helper](../src/regshape/cli/options.py) preserves existing
command parameter names, result schemas and operation semantics. The additive
`parse_artifact_ref` entry point preserves the old
`parse_image_ref(image_ref=...)` interface and parser grammar.
Docker's local-image `--image / -i`, layout layer `--index / -i`, destination
option names, and blob split target flags remain unchanged.

The [migration guide](guides/artifact-options.md) covers the exact command
matrix, old/new spellings, errors and Python usage. This does not implement
full artifact pull ([#91](https://github.com/toddysm/regshape/issues/91)),
canonical-reference redesign ([#88](https://github.com/toddysm/regshape/issues/88)),
or manifest GET progress ([#90](https://github.com/toddysm/regshape/issues/90)).
Full artifact pull is separately Implemented; canonical-reference redesign remains Planned. Manifest
GET progress is now separately Implemented under #90; it was not part of the
artifact-option work.

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
and 3.12. The final artifact-documentation head
`ecb06871fb0285bf1f5c55b9c308e76988c33041` passed
**3,140 tests per version**, plus **1,302 minimum-Click 8.1.0 compatibility tests**
and **1,469 legacy Requests/urllib3 tests** on Python 3.10, in
[run 42135118482](https://github.com/toddysm/regshape/actions/runs/42135118482),
with successful Python/Actions CodeQL analyses and current-head agent review.
Its signed merge has the same tree as the tested head. The existing auth-command
Click deprecation warning remains unrelated to this feature.
The same run passed **674 macOS filesystem/command tests** and **266 Windows
parser/unsupported-platform tests**, with **408 native-operation skips** on
Windows. Those skips do not establish native Windows support.

Focused production type checks and lint/security baseline comparisons found no
new findings. Documentation validation checked links, command help, callback
signatures, exact display snapshots, and executable examples with mocked
transport. Final artifact documentation verified **216 local links/anchors**,
seven Python and 41 Bash snippets, mocked directory/archive round trips,
help/signatures/result shapes and actual renderer captures; **414 scoped local
tests** also passed. Independent security review during graph delivery verified
the redirected-challenge credential fix; it is not blanket registry/OS certification.
No documentation builder or Markdown linter is configured. The tests
mock registry and Docker boundaries; they do not certify every live registry
or daemon.

Update this inventory when related work lands or a tracking decision changes.
Do not mark a feature Implemented merely because its design or a pull request
exists. Completion requires all required implementation/documentation items
and a merged inventory close-out before the tracking issue is closed.
