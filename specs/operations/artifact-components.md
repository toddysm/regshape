# Config and zero-based layer downloads: requirements and engineering design

- Status: proposed design revision 1; not approved or implemented.
- Feature: [#112](https://github.com/toddysm/regshape/issues/112).
- Design work: [#198](https://github.com/toddysm/regshape/issues/198).
- Target: 0.2; no release, implementation or merge authorization is implied.
- Updated: 2026-11-22 (UTC).
- Source baseline: [abc6daa](https://github.com/toddysm/regshape/commit/abc6daae216159e1d902145a8eaadae1a5f79b74).
- Planning format: full, combined product requirements and engineering design.
- Testing mode: hosted GitHub CI and automated review; local checks precede publishing.

## Part I: Product requirements

### 1. Executive overview

Let a registry operator or Python caller save the configuration or a numbered
content layer of an artifact with one operation. They should not have to retrieve
descriptor metadata, copy its digest and issue a second command themselves.
This next-release capability supports both images and non-image artifacts.
It preserves exact bytes and reports progress without requiring a container daemon.

### 2. Problem and customer evidence

The owner captured the request in [#112](https://github.com/toddysm/regshape/issues/112)
while planning complete-artifact downloads. Existing
[manifest field extraction](../cli/manifest.md#manifest-get) returns descriptors;
[blob get](../cli/blob.md) needs an already-known digest. The current workaround
therefore requires two command invocations and a descriptor-selection step.
Complete-artifact pull acquires an entire graph when only one component is wanted.

The owner subsequently chose `config` and `layer` as resources, reserved
`artifact` for meta operations, and confirmed new-file-only output with cleanup,
no overwrites and no raw stdout. These decisions are captured in the feature ledger.

> NOTE: Evidence is one maintainer request and the verifiable existing workflow,
> not three independently researched customers. No adoption, frequency or market
> measurements were supplied; this document does not manufacture them.

### 3. Customer scenarios

**S1: Inspecting a config.** The maintainer has an artifact tag and needs its
configuration for an independent inspection tool. Previously they extracted the
config descriptor and copied its digest into `blob get`. They now run
`config get` with the artifact reference and a new filename. After manifest
resolution and byte progress, the verified file contains the original bytes,
including whitespace and a missing final newline. A non-JSON config remains valid.

**S2: Saving one layer.** An operator needs the first content layer of a
single-layer package without acquiring its entire graph. They select index 0
with `layer get`. On a multi-layer artifact, they can select the first or last
position in manifest order. The file remains compressed or opaque as stored;
the operation neither extracts it nor assumes it is a filesystem.

**S3: Stable selection and explicit rejection.** A Python integrator starts from
a moving tag. Once its manifest is received, the selected descriptor and recorded
manifest digest stay fixed even if the tag changes. If the target is an index,
the integrator instead gets an actionable error and must provide an explicit
child-manifest reference; the tool never guesses a platform or child.

**S4: Failure and recovery.** The operator supplies an existing output path, an
invalid layer position, or encounters missing/corrupt content, disk exhaustion
or cancellation. The operation reports failure, produces no success result and
removes only its own incomplete staging. Existing files are untouched. If a
failure occurs after verified publication, the complete file remains and the
operator is told not to infer success from file existence alone.

### 4. Outcomes, requirements and scope

The outcome is one explicit, verifiable component acquisition rather than manual
descriptor plumbing or an unnecessary full-graph download. All R1-R12 are must-have.

| ID | Requirement |
| --- | --- |
| R1 | Expose `config get` and `layer get`; reserve `artifact` for whole-artifact/meta operations. |
| R2 | Support registry-embedded tag/digest targets through `--artifact/-a` and equivalent direct SDK inputs. |
| R3 | Receive one simple manifest once; pin selection to its bytes and resolved digest. |
| R4 | Download exactly the selected descriptor's digest and verify its declared nonnegative integer size. |
| R5 | Preserve arbitrary config/layer payload bytes; no JSON reserialization, decompression, extraction or execution. |
| R6 | Reuse actual manifest/blob byte observations, shared rendering and progress suppression/separation rules. |
| R7 | Preserve credential/auth, redirect, retry, callback, cancellation and primary-error semantics; no daemon. |
| R8 | Reject negative/noninteger layer selections, empty layer lists and out-of-range positions; accept layer 0 of a single-layer manifest. |
| R9 | Reject indexes with guidance to target an explicit child manifest; no platform selection. |
| R10 | Require a new output file; no raw stdout, overwrite, append, verify-only or auto-generated filename. Clean up owned incomplete staging and report cleanup failures. |
| R11 | Return a typed immutable result only after integrity, publication and owned cleanup; define errors and post-publication ownership explicitly. |
| R12 | Deliver deterministic SDK/CLI tests, compatibility coverage, final user guide, command/API references and navigation. |

Non-goals: graph traversal, subjects/referrers acquisition, implicit child selection,
embedded-data or descriptor-URL acquisition, batch selection, persistent resume,
payload transforms, config editing, new authentication flows, canonical-reference
redesign, registry writes, dependency upgrades and release publication.
Existing related features and deferred resume remain separately tracked in
[#91](https://github.com/toddysm/regshape/issues/91),
[#88](https://github.com/toddysm/regshape/issues/88) and
[#142](https://github.com/toddysm/regshape/issues/142).

### 5. Existing internal solutions and reuse

Extend the library, not the existing descriptor commands. Reuse
[raw manifest receipt](../../src/regshape/libs/manifests/download.py),
[strict document parsing](../../src/regshape/libs/artifacts/graph.py),
[verified bounded blob receipt](../../src/regshape/libs/blobs/operations.py),
[safe private storage/publication](../../src/regshape/libs/artifacts/storage.py),
[reference validation](../../src/regshape/libs/refs.py),
[download controls](../../src/regshape/cli/downloads.py) and
[shared progress rendering](../../src/regshape/cli/progress.py).
Do not change the existing commands' defaults or output shapes.

### 6. Competitive analysis

> NOTE: No external competitive research was requested or supplied. This small
> additive capability is justified by the existing two-command workaround;
> no claim of market differentiation is made.

### 7. Desired customer experience

CLI shape:

```sh
regshape config get -a registry.example/team/package:stable -o ./config.bin
regshape layer get -a registry.example/team/package:stable --index 0 -o ./layer.bin
```

Both commands require `--artifact/-a` and `--output/-o`. `layer get` also requires
`--index N`; there is no implicit position. Use the shared artifact-option helper,
including its compatible `--image-ref/-i` aliases and conflict rejection.
The shared parser's omitted-tag default remains `latest`; explicit tag and digest
targets are tested. The registry must be embedded.

The initial contract is file-only: `--output -` is a usage error, not a file named
`-` or an stdout stream. Existing entries, including directories and dangling
symlinks, are refused. Parent directories must already exist. An existing-file
error advises selecting a new path or explicitly removing the old file.
No `--force` or automatic overwrite/recovery option is added.

Normal stdout contains a completed metadata summary. `--json` instead emits one
completed result object. Raw component bytes go only into the selected file;
progress, warnings and telemetry remain on stderr. JSON output/telemetry,
nonterminal stderr and `--no-progress` suppress animation, not verification.

Direct Python callers use the same selection, integrity, limits and file lifecycle
without importing Click or any CLI module. UI and hosted agent services are N/A:
no new graphical or hosted service surface is introduced. Bicep, Terraform and
Helm are N/A because this capability does not provision infrastructure.

### 7A. Sample output

The hostname and digest below are synthetic. This is the proposed exact text
shape, with paths and metadata substituted from the completed result:

```text
$ regshape config get -a registry.example/team/package:stable -o ./config.bin
Downloaded config.
Source: registry.example/team/package:stable
Resolved: registry.example/team/package@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Output: /work/config.bin
Digest: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
Size: 12 bytes
```

For a layer, the first line is `Downloaded layer 0.`. JSON field names/types are
specified in PM-2. There is no success transcript or JSON object on failure:

```text
$ regshape layer get -a registry.example/team/package:stable --index 1 -o ./layer.bin
Error [registry.example/team/package:stable]: Layer index 1 is out of range for 1 layer; valid index: 0.
$ regshape config get -a registry.example/team/package:stable -o ./existing.bin
Error [registry.example/team/package:stable]: Output already exists. Choose a new --output path or explicitly remove the existing file.
$ regshape layer get -a registry.example/team/package:stable --index 0 -o ./layer.bin
Error [registry.example/team/package:stable]: Component selection requires a simple manifest. Target an explicit child-manifest digest.
```

The first/third errors exit 1 because selection depends on the received manifest.
Missing arguments, negative/noninteger `--index` and `--output -` are Click usage
errors with exit 2. Existing-output preflight is an operational failure, exit 1.

### 8. Goals and acceptance metrics

| Outcome | Baseline | Acceptance target and time | Measurement |
| --- | --- | --- | --- |
| Remove manual descriptor plumbing | Two commands plus selection | One new CLI invocation or SDK call by feature completion | Deterministic successful workflows |
| Avoid acquiring unrelated content | Complete pull traverses the graph | One accepted manifest and one selected blob per full-stream success, excluding auth/retries | Recorded fixture requests |
| Preserve integrity/output safety | Existing primitives verify blobs; no combined selection API | Every specified corruption, selection and pre-publication failure fails without publishing incomplete bytes | Failure matrix and filesystem assertions |
| Preserve compatibility | Current command test suite on main | Existing command outputs/defaults and direct SDK import isolation remain green before each merge | CI tests and fresh-process imports |

Range mode may use HEAD/range requests; no blanket two-HTTP-request claim applies.
Guardrails are bounded metadata, one selected blob, existing retry/concurrency
budgets and zero overwrite/payload-transform behavior. Design approval, runtime
delivery and final documentation are separate exit gates in section 14.

> NOTE: Commercial adoption, support-load and per-customer churn targets are not
> provided. This local tool adds no phone-home usage collection or backend.

### 9. Pricing

N/A: no monetization, billing meters, package-price or registry-price change.
Registry/network egress and local storage remain the caller's costs.

### 10. Performance and scale

One operation selects one blob. Defaults reuse existing budgets: 16 MiB per
manifest, 1 TiB selected-blob cap, 128 JSON depth and 500,000 descriptor edges.
Expose `--max-manifest-bytes` and `--max-blob-bytes` as positive integer overrides.
Reject an advertised blob size over its cap before the blob request. Require
exact size equality on receipt; enforce the cap before writes, including size 0.
A zero-byte blob is valid only when its digest verifies the empty payload.

Read buffers default to 65,536 bytes; the complete blob is never buffered in
memory. Parsing memory is bounded by the manifest cap and existing JSON/edge
limits. Private output is at most the manifest bytes plus selected blob bytes;
publication uses a same-filesystem hard link, not another full content copy.
Optional range scratch and transport concurrency retain existing policy bounds.

> NOTE: No new p50/p95/p99 latency or throughput SLO was supplied. Registry/network
> variability prevents inventing one. Acceptance measures request selection,
> bounded receipt and exact bytes rather than claiming a new performance SLA.

### 11. Customer observability and troubleshooting

Actual bytes are visible during manifest resolution and selected config/layer
receipt on interactive stderr. Receipt is not final publication success.
The metadata result reports immutable manifest/blob identity, size and destination.
Existing opt-in timings, scenario metrics and bounded download status remain usable.

| Detect | Diagnose | Resolve |
| --- | --- | --- |
| Index/selection diagnostic | Inspect manifest metadata and its ordered layer list | Supply an explicit child reference or valid index |
| Existing/inaccessible output | Check filename, parent permissions and filesystem support | Choose a new path or correct the trusted parent |
| Registry/auth/missing-blob error | Check stored credentials and repository access | Use `auth login` or an accessible target, then retry |
| Digest/size/cap failure | Registry content disagrees with descriptor or configured cap | Fix source content; raise a cap only for an intentionally larger verified object |
| Cancellation/cleanup diagnostic | Check exit status and explicitly reported retained staging | Retry using a new file; inspect retained owned staging before manual cleanup |
| Error after publication | Complete verified output may already exist | Inspect/verify the file; do not rerun blindly into the same path |

### 12. Security

Reuse Docker-compatible credential resolution and registry Bearer challenges,
requiring repository `pull` scope only. Public anonymous reads remain supported.
No new identity, daemon, service principal or permission change is needed.
TLS verification is on unless the caller explicitly selects existing insecure mode.
Safe redirect behavior stays with transport/raw-receive helpers; never fetch
descriptor `urls` or execute payloads. References reject URL/userinfo/traversal
forms rather than sanitize them.

Local output/staging is caller-controlled. Reuse private-namespace ownership,
ACL, no-follow, handle-relative identity and exclusive-publication protections.
These are not a sandbox against hostile same-user/root mutation. Native safe
publication supports Linux/macOS on compatible filesystems; Windows fails closed
before credentials/network or creating output. No encryption-at-rest/key-management
feature is introduced; callers own filesystem encryption and content retention.
Progress/results must not contain tokens, authorization headers, signed URLs or
payloads. Existing opt-in request diagnostics remain sensitive and must be handled
according to the [telemetry guidance](../../docs/library/telemetry.md).

### 13. Risks and owners

The implementing maintainer owns all mitigations: moving tags (pin received bytes),
size-coercion bugs (strict parser), premature success (deferred completion),
publication/cleanup races (existing ownership helpers), unsupported filesystems
(fail closed) and shared-command regressions (compatibility test gates).

> NOTE: Safe Linux/macOS-only publication is a proposed compatibility constraint
> inherited from the selected storage mechanism, not a newly approved Windows
> promise. The owner reviews this constraint at the design gate.

### 14. Rollout and milestones

1. **Design, started 2026-11-22 UTC:** requirements, child breakdown, inventory,
   local self-review and hosted checks/review. Exit: explicit owner approval
   and an authorized design merge. Implementation is blocked until then.
2. **Runtime delivery for 0.2:** SDK selection/storage, then CLI/progress and
   compatibility. Exit: acceptance tests, resolved review feedback and valid
   hosted checks for each head; no unverified or incomplete result.
3. **Documentation/readiness for 0.2:** guide, command/API references and navigation.
   Exit: all required children complete, owner-visible docs match runtime and
   inventory can be moved to Implemented under the normal close-out PR gate.

There is no independently hosted internal/private/public preview or regional
rollout. Package release follows the existing release workflow only after separate
authorization; no release date or GA/customer adoption commitment is supplied.

## Part II: Engineering design

### A. Abstract

Add two resource-oriented read commands and equivalent framework-independent
Python functions that save a config or zero-based layer from a simple registry
manifest. The orchestration receives a raw manifest once, validates its supported
shape, records its immutable digest, selects a descriptor and streams only that
blob through existing integrity and download-policy helpers. It does not traverse
indexes, fetch subjects, interpret payload formats or use descriptor URLs. A
caller-selected new file is required. Verified bytes are privately staged beside
the destination and published with the existing atomic no-replace hard-link
operation; failures before publication remove only owned staging, while complete
output remains caller-owned after publication even if later diagnostics fail.
Existing immutable manifest/blob observations and shared CLI rendering report
actual byte progress without turning receipt into command success. Credential
handling, redirects, retries, cancellation and primary-error preservation remain
with the current transport and download primitives. This is additive work for
0.2; existing descriptor extraction, blob get and complete-artifact pull remain
unchanged, and this proposed design awaits explicit owner approval.

### B. Pre-requisites

Part I is the current proposed requirements source and
[#198](https://github.com/toddysm/regshape/issues/198) owns its approval.
Earlier [#120](https://github.com/toddysm/regshape/pull/120) is merged but approved
complete-artifact operations, not this feature. Reuse completed foundations on
main from #90, #91, #131 and #163; #88 is related, not a prerequisite.
No implementation starts until this full design and its native children are approved.

### C. Introduction

Existing SDK primitives already resolve references, receive raw manifests, verify
streamed blob integrity and publish complete private files. What is missing is
the small orchestration selecting a component from a pinned simple manifest and
presenting it as the owner's chosen `config`/`layer` resources. Implementing that
in the SDK first removes duplicate CLI-only logic and fits the next-release request.

### D. Chosen design and component lifecycle

Proposed new modules are `src/regshape/libs/components.py` (shared orchestration,
result and preflight), `src/regshape/cli/config.py` and `src/regshape/cli/layer.py`
(resource groups), and a small shared `src/regshape/cli/components.py` presenter.
Register the two resource groups in [CLI main](../../src/regshape/cli/main.py)
and export the library module from [libs](../../src/regshape/libs/__init__.py).
These new paths are proposals, not files already on main.

```text
Python caller ----------------------+
                                    v
config get / layer get --> library component preflight/orchestration
      |                             |
      |                    raw manifest receive + strict parse
      |                             |
      |                    pinned config / layer descriptor
      |                             |
      |                    shared verified blob download
      |                             |
      |                    private staging --> exclusive publication
      v                             |
shared renderer <-- immutable observations
      |
metadata stdout only after operation and CLI cleanup
```

Lifecycle:

1. Validate source syntax, component/index, limits, callbacks, policy and output
   before side effects. Preflight output support/parent/collision before creating
   the command's client (therefore before credential resolution).
2. Open safe parent handles and a private namespace on the output filesystem.
   Receive one manifest using `download_manifest` and `DEFAULT_MANIFEST_ACCEPT`.
   Preserve manifest byte progress, digest verification and bounded error receipt.
   Immediately after successful receipt/close, call `staging.register(manifest_name)`
   for this invocation's exclusively created file, before parsing or any operation
   that can fail. The receiver does not update SafeDirectory's ownership ledger.
3. Parse that bounded raw file with `parse_document`/its strict helpers.
   Reject indexes explicitly. Supported OCI and Docker schema-2 simple manifests
   permit arbitrary valid config/layer media types. Select config by role, or
   the Nth layer edge in manifest order; subject edges do not count.
4. Keep the descriptor digest, integer size and media type as immutable scalars.
   Do not reread the tag, traverse children/subject or satisfy receipt from
   embedded `data`/`urls`; the selected repository blob must exist.
5. Enforce selected size against limits. Call `get_blob` into private storage
   with `total_size=descriptor.size`, a positive bounded `max_bytes` and borrowed
   parent handle. Size 0 uses a positive receiver cap while strict total size 0
   forbids any bytes; do not pass an invalid zero cap.
   Immediately after successful receipt/close, call `staging.register(blob_name)`
   before publication. Register only newly received files owned by this invocation;
   do not adopt arbitrary existing files. Both downloaded files must be in the
   ledger so publication can locate the blob identity and cleanup can remove the
   manifest and any unpublished blob. If registration/identity verification itself
   fails, report the failure and any retained staging rather than blindly unlinking
   unregistered or replaced entries.
6. After verified blob response/file closure, publish with `SafeDirectory.publish_file`.
   Existing destinations lose neither content nor identity even in a collision race.
   No pathname-based overwrite, copy fallback or cross-filesystem publication.
7. Clean up owned manifest/staging and close owned handles; preserve any primary
   exception while reporting secondary cleanup failures. Return the immutable
   completed result only when the complete operation succeeds.

The borrowed RegistryClient remains caller-owned; existing pooled receipt owns
its worker contexts. No caller-owned stdout/client/callback is closed.
Versioning is additive Python/CLI surface in 0.2; no remote endpoint, persisted
schema, cache key, queue or service API version is added.

### PM-1. Traceability

| Requirements / scenarios | Design and validation |
| --- | --- |
| R1-R2, S1-S2 | Resource registration/shared selector and direct SDK signatures |
| R3, S3 | One raw manifest receive, strict parsing and pinned resolved reference |
| R4-R5, S1-S2 | Descriptor-addressed verified blob receipt; exact-byte fixtures |
| R6-R7, S4 | Existing typed observers, shared renderer, transport and cleanup contracts |
| R8-R9, S2-S3 | Preflight integer/index checks, count checks, explicit index rejection |
| R10-R11, S4 | New-file-only private staging, no-replace publication and frozen result |
| R12, S1-S4 | PM-7 test matrix and required final documentation child |

No new feature is introduced without an R1-R12 justification. Platform constraints
and missing market/SLO evidence remain explicit approval notes, not silent deferrals.

### PM-2. Python and CLI contracts

Proposed public SDK surface in `regshape.libs.components`:

```python
def preflight_component_download(
    registry: str, repo: str, reference: str, output_path: str | Path, *,
    component: Literal["config", "layer"],
    layer_index: int | None = None,
    limits: ArtifactLimits | None = None,
) -> None: ...

def download_config(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    limits: ArtifactLimits | None = None,
    chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult: ...

def download_layer(
    client: RegistryClient, repo: str, reference: str, output_path: str | Path, *,
    index: int,
    limits: ArtifactLimits | None = None,
    chunk_size: int = 65536,
    progress_callback: ProgressCallback | None = None,
    manifest_progress_callback: ManifestProgressCallback | None = None,
    phase_callback: ArtifactPhaseCallback | None = None,
    download_options: DownloadOptions | None = None,
    download_status_callback: Callable[[DownloadStatus], None] | None = None,
) -> ComponentDownloadResult: ...
```

`ArtifactLimits` is reused without adding a policy type: apply its manifest,
selected-blob (`max_total_bytes`), JSON depth and descriptor-edge budgets; unrelated
graph/archive fields do not expand scope. The CLI maps `--max-blob-bytes` to
`max_total_bytes`. `chunk_size` and byte limits must be positive, non-boolean
integers. Layer index must have `type(index) is int` and be nonnegative; do not
coerce booleans, floats or strings. A layer operation cannot accidentally select
config when an invalid `None` index reaches it.

SDK preflight uses the shared strict reference classifiers/validators and safe
filesystem/collision checks without acquiring credentials or creating output.
The download entry points repeat preflight using `client.config.registry`; CLI
preflight is not a trust substitute or filesystem reservation.

Python callers can start from the same registry-embedded artifact string and use
the existing `parse_artifact_ref` helper to supply the borrowed-client API:

```python
from regshape.libs.components import download_layer
from regshape.libs.refs import parse_artifact_ref
from regshape.libs.transport import RegistryClient, TransportConfig

registry, repo, reference = parse_artifact_ref("registry.example/team/package:stable")
client = RegistryClient(TransportConfig(registry=registry))
result = download_layer(client, repo, reference, "./layer.bin", index=0)
```

The proposed API borrows the client; the caller retains its normal transport
lifetime responsibilities. Config uses `download_config` with the same inputs
and no index. Example destinations must be new files under supported trusted parents.

The frozen `ComponentDownloadResult` contains only immutable scalar fields:

| Field | Python / JSON type | Meaning |
| --- | --- | --- |
| `source` | str / string | Registry/repository with the supplied tag or digest |
| `resolved_reference` | str / string | Registry/repository with verified manifest digest |
| `component` | Literal config/layer / string | Selected resource |
| `layer_index` | int or None / integer or null | Selected zero-based position; null for config |
| `digest` | str / string | Verified descriptor blob digest |
| `size` | int / integer | Exact verified blob byte count |
| `media_type` | str / string | Selected descriptor media type, not a payload interpretation |
| `output_path` | str / string | Absolute destination path |

Do not return the existing mutable Descriptor inside the frozen result, an open
handle, mutable dict or a partial-result success sentinel. A CLI JSON result
serializes exactly these eight fields with existing JSON formatting.

Common CLI options: required `--artifact/-a`, required `--output/-o`, `--json`,
`--no-progress`, positive `--chunk-size` (default 65536),
`--max-manifest-bytes` (16777216), `--max-blob-bytes` (1099511627776),
existing telemetry flags and the unchanged shared download options
(`--concurrency`, `--ranges-per-blob`, `--range-size`, `--sequential`,
`--legacy-transport`, `--download-retries`, `--scratch-dir`).
Only `layer get` adds required `--index` as `click.IntRange(min=0)`.
No `--part`, `--platform`, `--force`, `--raw` or per-command credentials.

New library validation/selection/output errors use a `ComponentError` extending
`RegShapeError` in [errors](../../src/regshape/libs/errors.py), with safe actionable
messages. Existing AuthError, ManifestError, BlobError and transport exceptions
retain their identity. Callback/cancellation and unexpected exceptions propagate
unchanged. Only narrowly identified operation-owned filesystem/strict-parsing
errors may become ComponentError with the original cause; callback errors of the
same base type must not be mistaken for operation errors.
CLI expected-operation failures use existing `emit_error`, exit 1; usage exit 2;
interruptions retain Click's nonzero behavior and no result.

All methods are additive. Reads may be retried by existing transport policy; the
overall operation is not automatically retried after publication. Reusing a
completed destination is a conflict, not idempotent overwrite.

### PM-3. Metrics, usage and cost

Use existing manifest/transfer observations for consumed bytes, trusted total and
verification state, scenario timing for the complete operation, method timing
for receipt, and pooled status for strategy/retry/request metrics. Measure the
section-8 outcomes in deterministic fixtures and current-head CI.
No backend, durable usage database, customer identifier or phone-home telemetry
is added. Opt-in local telemetry logs remain caller-controlled, not a new
longitudinal adoption service.

> NOTE: Per-customer adoption/churn measurement is not part of this local-tool
> request. The cloud-service rubric's longitudinal backend measurement is an
> explicit gap, not permission to introduce an analytics backend.

Cost method: count actual registry requests/retries/ranges and transferred bytes
using existing status/timing signals; apply caller/provider egress rates externally.
Local cost is one selected blob plus bounded metadata/scratch, elapsed CPU/time
and disk space, not a promised currency amount or free registry traffic.
Results/events exclude high-cardinality secret-bearing URLs and payloads.

### PM-4. Authentication, authorization and networking

SDK clients use the existing credential configuration and Bearer challenge cycle;
CLI uses credentials from `auth login`. Request only repository pull access.
No identity/permission changes or inbound service exposure. Egress is registry/
token-service HTTPS (configured ports and proxies remain supported); authenticated
redirect credentials follow current stripping/origin rules. Private registries
require the caller's existing network route/firewall access, not new VNet,
Private Link, NSG or cloud deployment. Secret rotation stays with existing auth.
Do not fetch untrusted descriptor URLs or log tokens and signed URLs.

### PM-5. Error handling, ownership and edge cases

| Failure | Outcome and recovery |
| --- | --- |
| Invalid selector/index/options/callback/policy | Fail before credential/output/network effects where detectable; fix input |
| Existing/dangling output or unsafe parent | Fail preflight; no overwrite or symlink following |
| Index, malformed/unsupported manifest, missing config | No blob request; explicit selection/document error |
| Empty layers or N >= count | No blob request; report zero-layer or valid position range |
| Missing/auth-inaccessible blob | Preserve upstream failure; clean owned staging |
| Invalid digest, declared size or actual bytes | Explicit integrity error; never publish incomplete/corrupt content |
| Tag changes | Continue with received immutable descriptor, never refetch tag |
| Payload/error exceeds cap | Fail before overflow write; no unlimited buffering |
| Response/short-write/disk-full/publication collision | Fail explicitly; preserve other owners' files |
| Observer/cancellation exception | Abort acquisition and clean only owned staging; preserve exception identity |
| Cleanup failure while failing | Report secondary error; do not replace the primary or imply complete cleanup |
| Error/cancellation after publication | Retain the complete public file; no success result, no rollback of caller output |

The hard-link syscall is the irreversible publication boundary, even if
cancellation arrives before Python observes its return. No broad catch or cleanup
path deletes a public name after that boundary. Identity-mismatched replacements
are retained and reported, not adopted as this operation's owned staging.
Documentation must distinguish complete retained output from incomplete retained
staging and explain that nonzero exit does not guarantee absence of a file.

No extra retry loop: manifest uses existing client behavior and blob uses shared
download/retry/range policy. Policy is resolved before effects. When range scratch
is omitted, use the operation's trusted private namespace as the default, as
complete-artifact pull already does. Preserve explicit legacy/custom-client
compatibility and explicit failure for unsupported pooled/status requests.

### E. Risk identification and assessment

| Area | Assessment | Mitigation |
| --- | --- | --- |
| Breaking/compatibility | New CLI registration and shared helpers could regress old commands | Additive APIs, unchanged old flags/results; regression tests |
| Security/auth | Untrusted reference, metadata, redirect and filesystem inputs | Shared strict validators, bounded receipt, pull-only credentials, no-follow publication |
| Privacy | Payloads/configs and opt-in diagnostics may be sensitive | No payload/credential events; private staging; caller retention |
| Dependencies | Existing graph/storage/download APIs are reused | No new hard external dependency; targeted primitive compatibility tests |
| Infrastructure/buildout | Filesystems may not support safe publication | Linux/macOS capability checks; explicit refusal, no unsafe fallback |
| Deployment | CLI could land before SDK or docs lag | Dependency-ordered children and mandatory final docs |
| Multi-tenancy/noisy neighbor | No new shared service; callers can compete for disk/network | Invocation caps and existing transport concurrency; trusted parent contract |
| Regression | CLI, refs, parser, receiver, renderer and telemetry lifetimes | Component tests plus corresponding existing suites and fresh-process import isolation |

### F. Breaking changes

None intended: new resources are additive. Existing `manifest get --part`,
`blob get` and `artifact pull` keep their semantics, output schemas and defaults.
New-file/file-only rules apply only to this new surface. Shared helper changes
must remain additive and are covered by PM-7, including minimum Click/legacy
transport tests. No persisted data, IaC or schema migration/deprecation.

### G. Security risk management

Trust boundaries: caller inputs -> validated manifest -> selected descriptor ->
bounded registry bytes -> verified private file -> public filename. Each crossing
checks shape, digest/size or ownership as appropriate. Reuse SHA-256/SHA-512 and
existing TLS; no new crypto or CMK integration. Limit metadata/depth/edges/blob
bytes and range scratch; no command interpolation, path traversal, execution or
foreign URL fetching. The private namespace excludes untrusted different-user
access, not hostile same-user/privileged actors. Preserve safe resource cleanup
and review diagnostics for credentials without changing opt-in telemetry policy.

### H. Privacy and data sovereignty

Payloads, configs and references may be private. Store staging under the existing
private namespace until completion/cleanup; the completed file and existing
opt-in logs belong to the caller. No backend analytics, backup, cross-region
replication or new retention policy. Data travels only to the caller-selected
registry/auth/allowed redirect destinations and local output filesystem; the
caller selects compliant registry/network/storage locations. Do not claim
regional compliance certification.

### I. Operational observability

SDK callbacks are synchronous, frozen snapshots on the caller/observer thread,
never mutable job instructions. Existing manifest RECEIVED means receipt/verification
and close, not parsing or overall success. Blob progress uses requested digest and
declared total, including config and zero-byte cases. Retries keep existing absolute
counter/attempt semantics; no artificial progress from descriptor size alone.

Delay the forwarded TransferPhase.COMPLETED until verified publication and owned
operation cleanup; receipt completion transitions to COMMITTING while publication
is pending. Announce ArtifactPhase.PUBLISHING through the optional phase callback
before the publication boundary. If that callback fails, no file is published.
Never recursively notify an observer that already failed; preserve exact exceptions.
An exception from the final completion callback occurs after publication, leaves
the verified file intact and yields no successful return.

CLI adapters may label shared rendering as Config/Layer and render manifest
processing/local publishing, but cannot select descriptors, own acquisition or
implement file cleanup. Keep metadata stdout outside display/telemetry cleanup
using the existing after-telemetry pattern. A renderer/telemetry/output failure
must remain failure even when publication has already succeeded.

Health signals: exit/exception outcome, verified byte count, completed immutable
metadata and actual bytes/timings/status. Existing errors/status support the
section-11 triage paths; no new hosted alerting or invented availability SLO.

### J. Dependencies

Reuse existing internal refs, strict artifact parser, storage/native safety,
manifest/blob receipt, progress and transport modules. They introduce no new
third-party runtime package or registry capability requirement. Dependencies are
one-way from components to existing libraries; libraries never depend on CLI.
Registry availability, authentication, redirects, throttling and range support
keep their current failures/fallback policy; see PM-5. Filesystem support introduces
the explicit platform constraint in section 12 and failure gate in E.

### K. Deployment and buildout

Merge SDK before CLI, then final docs. Existing package build/install and release
automation is sufficient: no service deployment, regional buildout, backfill,
registry configuration or manual infrastructure step. No version bump, tag, release
event or workflow suppression is authorized by this design. Publish through the
existing release process only with separate owner approval.

### L. Multi-tenancy

N/A as a service: this is an invocation-scoped local library/CLI, not a multi-tenant
backend. Concurrent local processes retain separate private namespaces and atomic
output collision handling. Existing transport ceilings and byte budgets limit
one invocation; no global cross-process fairness guarantee is added.

### M. Regression and compatibility

Assess CLI main/options (existing registration/aliases), refs (unchanged parser
defaults), graph parsing (supported simple/image/non-image shapes), blob/manifest
receipt (integrity/auth/retry/cleanup), storage (no-replace identity safety),
renderer (stdout/stderr/structured suppression), telemetry (primary errors and
late-output lifetimes) and imports (SDK without Click).
Use existing manifest/blob/artifact/progress/auth/transport suites alongside new
component tests. Do not rewrite legacy symbols or change established defaults.
Older installed versions simply lack the new commands; documentation marks 0.2
targeting and advises checking installed help. No regional partial deployment.

### N. Rollback

Revert additive component APIs/resource registration and their docs under the
normal reviewed PR gate; there is no schema/config migration. Existing commands
remain a workaround throughout. Verified published files are caller-owned and
must never be removed as rollout rollback. Private incomplete staging from a crash
is not resumable or automatically adopted. Existing legacy transport flag remains
an acquisition rollback option, not permission to weaken publication safety.

### PM-6. Work breakdown and estimates

Exactly one final user-documentation item follows the runtime children:

| Native child | Deliverable | Depends on |
| --- | --- | --- |
| [#198](https://github.com/toddysm/regshape/issues/198) | Full design and inventory | Owner approval gate |
| [#199](https://github.com/toddysm/regshape/issues/199) | Verified component SDK | Approved/merged #198 |
| [#200](https://github.com/toddysm/regshape/issues/200) | Config/layer CLI and progress | #199 |
| [#201](https://github.com/toddysm/regshape/issues/201) | Required final user documentation | #199 and #200 |

Design is a separate PRD child; implementation children depend on approved/merged design.
Estimated maintainer engineering days are planning estimates, not promised dates:

| Deliverable/components | Implement/author | Tests/review | Delivery/rollback | Dependencies |
| --- | --- | --- | --- | --- |
| Full design and inventory | 0.5 | 0.5 | 0.25 | None; owner gate |
| SDK selection, strict receipt and publication | 1.5-2 | 1-1.5 | 0.25 | Approved design |
| Config/layer CLI, shared rendering/options and compatibility | 1 | 0.75-1 | 0.25 | SDK |
| Final guide, command/API references and navigation | 0.75 | 0.5 | 0.25 | Both runtime children |

Long poles: existing helper error/ownership contracts, delayed callback completion,
zero-size limits and native filesystem/cancellation fault injection. No outside
team dependency or new infrastructure. Update estimates rather than silently
relaxing gates if those assumptions fail. Native issue links are in the feature
ledger; each child has complete acceptance criteria.

### PM-7. Test plan and CI

Deterministic fixture coverage, mapped to R1-R12/S1-S4:

| Coverage | Required assertions |
| --- | --- |
| Tags/digests/config | One manifest selection, immutable resolved digest, exact config bytes |
| Zero/one/many layers | Zero-layer error, layer 0 success, first/last ordering, out-of-range failure |
| Input validation | Negative/noninteger/bool/None index, missing output, `-`, selector conflicts and unsafe refs fail at the proper boundary |
| Non-image payloads | Arbitrary config bytes, opaque/compressed layer bytes without interpretation |
| Moving tags/indexes | No tag reread or descriptor mixing; explicit index rejection, no child fetch |
| Integrity | Missing/403/404, invalid descriptor types, strict digest, corrupt/short/long content, zero-byte verification, cap overflow |
| Output failures | Existing file/dir/dangling link, parent/symlink/support checks, short write, disk full, concurrent collision, identity replacement, receipt-to-ownership registration, and cleanup failure |
| Progress/callbacks | Actual manifest/config/layer bytes, typed immutable snapshots, caller thread, retries, terminal ordering, exceptions with preserved identity |
| Cancellation/lifetimes | Before/during receipt, before/during publication, after publication, response/handle/worker cleanup and primary-error precedence |
| CLI compatibility | Help/registration, required selectors, no-progress/non-TTY/JSON, stderr-only diagnostics, no success on error, telemetry/output-after-publication behavior |
| SDK isolation | Direct component operations in a fresh process with Click/CLI imports forbidden |
| Existing behavior | Manifest descriptor extraction, blob get defaults/output, complete pull and legacy/minimum-Click tests unchanged |

Use pytest mocks/loopback fixtures, not live credential-dependent registries.
New targeted component test files join the default `python -m pytest -q`
collection. Wire relevant cases into existing explicit minimum-Click,
urllib3-legacy and filesystem selectors; preserve their runtime dependencies.
[Tests workflow](../../.github/workflows/tests.yml) runs on PRs and main pushes:
Ubuntu Python 3.10/3.11/3.12 plus explicit compatibility/platform jobs.
Run native successful filesystem cases on Linux and the local Mac; Windows tests
assert explicit fail-closed behavior. Existing hosted platform jobs remain
unchanged unless a later separately approved workflow change is needed.

This design-only PR changes no executable behavior: validate its links, schema/
signature consistency and requirement matrix; normal hosted tests still run.
Each implementation PR adds focused tests that fail without its behavior, performs
local self-review, requests hosted Copilot review and resolves feedback before an
authorized merge. Required code scanning/quality/signature/thread rules remain
binding; never fabricate success or override protection.

Final documentation is a required child: explain resource-vs-meta commands,
tag/digest and zero-based examples, arbitrary payloads, index rejection, file-only
defaults, refusal to overwrite, cleanup/publication caveats, result/error shapes,
SDK observers, progress suppression, platforms and recovery. Update command/
library references, README/help navigation and guide links. No hollow unit tests
for documentation-only work; use existing docs build/lint/link tools if present,
otherwise record that absence and verify changed links locally.

### O. Next actions and launch checklist

- [ ] Approve this full design and native child scope, then authorize design merge.
- [ ] Implement/test/review SDK, then CLI, in separate instance-owned feature PRs.
- [ ] Complete the single final documentation child and its navigation/recovery guidance.
- [ ] Reconcile all children and inventory before closing the tracking issue.
- [ ] Keep release publication separate; partner/legal/cloud-launch collaboration
  is N/A for this additive local open-source capability.

### P. References

- [Captured feature requirements](https://github.com/toddysm/regshape/issues/112).
- [Complete-artifact design](../cli/artifact-pull.md).
- [Artifact API](artifacts.md) and [blob API](blobs.md).
- [Manifest progress design](../cli/manifest-progress.md).
- [Parallel download contracts](parallel-downloads.md).
- [SDK/CLI ownership inventory](../../docs/project-state.md).
- [Transfer callback reference](../../docs/library/transfer-progress.md).
