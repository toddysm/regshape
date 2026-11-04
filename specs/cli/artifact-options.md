# Artifact-oriented reference options: functional and engineering specification

- Status: approved design, revision 1; shared support is being implemented in
  #93. Production command integration and user documentation remain pending.
- Feature: [#87](https://github.com/toddysm/regshape/issues/87).
- Design work: [#92](https://github.com/toddysm/regshape/issues/92).
- Target: 0.2; this specification does not publish or schedule a release.
- Updated: 2026-11-04 (UTC).
- Source baseline: [70b060d](https://github.com/toddysm/regshape/commit/70b060d8938ea96e4915bef658725212b5ea5f9b).
- Validation mode: hosted GitHub Actions and agent-only reviews.
- Approval: the owner approved the complete design and breakdown;
  [design PR #96](https://github.com/toddysm/regshape/pull/96) is merged.

The repository's `specs/` tree is the home for functional and engineering
specifications. This document combines product requirements with the technical
design; it does not create a separate requirements tree.

## Part I: Functional requirements

### 1. Executive overview

Make RegShape's command-line interface (CLI) describe registry artifacts rather
than imply that all Open Container Initiative (OCI) content is a container image.
Introduce `--artifact / -a` on eight existing registry-reference commands.
Keep `--image-ref / -i` working, but identify those aliases as deprecated in 0.2
help and migration documentation. Do not add runtime deprecation warnings.

The primary beneficiaries are the repository owner and operators/scripts using
RegShape for both images and non-image artifacts. This is a naming and
compatibility change, not a new content format, download operation, or parser
grammar.

### 2. Problem and evidence

The owner reported in #87 that image-oriented flags misrepresent OCI registry
content, preferred `--artifact` over `--artifact-ref`, and requested an explicit
next-release deprecation notice on the old aliases. A source/Click inventory
found eight commands exposing `--image-ref / -i`, with `-a` unused on each.
Docker's two `--image / -i` selectors are a different, genuinely image-specific
surface.

Evidence comes from the owner's request, the actual command inventory, and
the [shared reference utilities](../../src/regshape/libs/refs.py). There is one
identified stakeholder, not three independently researched customers.

> NOTE: Broader customer/adoption research was not supplied. This small CLI
> quality change does not claim market validation or manufacture customers.

### 3. User scenarios

**S1: Maintainer inspecting an artifact.** The repository owner has a registry
package that uses an already-supported manifest format but is not a runnable
image. They open manifest help, see an artifact-oriented selector, provide the
same registry/tag reference they previously used, and receive the unchanged
manifest result. The naming no longer suggests an image-only restriction.

**S2: Operator preserving a script.** An operator upgrades to a version
containing this feature while retaining an existing `manifest get -i ...`
script. Its requests, output and exit behavior remain unchanged. Help and the
migration guide explain the new spelling without inserting warning lines into
automation output. The operator can migrate scripts at their own pace.

**S3: Operator combining command fragments.** An operator combines a new
`--artifact` option with an old `--image-ref` fragment. Identical values identify
one target and result in one operation. Different values fail with a specific
usage error before credential resolution or registry requests, avoiding an
accidental write/delete against whichever value appeared last.

**S4: Python integrator updating names.** A library consumer adopts
`parse_artifact_ref(artifact_ref=...)`; their older callers using
`parse_image_ref(image_ref=...)` still behave identically. This does not change
the reference grammar, returned tuple or downstream authentication.

### 4. Outcomes, requirements and scope

All requirements below are mandatory for this feature.

| ID | Requirement |
| --- | --- |
| R1 | Prefer `--artifact / -a` on the eight commands in the command matrix. |
| R2 | Retain `--image-ref / -i`; mark only these aliases deprecated in 0.2 help/docs; no runtime deprecation warning or removal date. |
| R3 | Resolve identical repeated values once; reject differing values with usage exit 2 before credentials/network. |
| R4 | Preserve valid single-target behavior, per-command restrictions, output schemas, requests, auth, progress and payload handling. |
| R5 | Add neutral parser naming while retaining the existing parser's calling conventions and behavior. |
| R6 | Preserve image-specific Docker selectors and specification/model names; use accurate artifact/repository wording elsewhere. |
| R7 | Ship tested aliases, help, developer contracts and final user migration documentation. |

Non-goals: positional arguments or blob-reference consolidation
([#88](https://github.com/toddysm/regshape/issues/88)); full artifact downloads
([#91](https://github.com/toddysm/regshape/issues/91)); manifest GET progress
([#90](https://github.com/toddysm/regshape/issues/90)); credential-store fixes or
guide work ([#86](https://github.com/toddysm/regshape/issues/86)); new media types,
transport policies, telemetry exporters, runtime dependencies or package releases.
These are tracked boundaries, not unfiled feature deferrals.

### 5. Existing implementation and reuse

Reuse Click, [reference parsing](../../src/regshape/libs/refs.py),
[formatting helpers](../../src/regshape/cli/formatting.py), and the existing
transport/library operations. The option declaration is currently repeated
across the affected command modules; share only the alias-resolution behavior.
Do not introduce a general configuration framework.

### 6. External conventions

Official documentation/source checked on 2026-11-04 shows positional primary
references rather than an `--artifact` target flag:

| Tool | Inspected syntax | Implication |
| --- | --- | --- |
| [skopeo inspect](https://github.com/containers/skopeo/blob/main/docs/skopeo-inspect.1.md) / [copy](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md) | `inspect IMAGE`, `copy SOURCE DESTINATION`, transport-prefixed references | Image-oriented language remains; named artifact selectors are not a common flag convention. |
| [crane manifest](https://github.com/google/go-containerregistry/blob/main/cmd/crane/cmd/manifest.go) / [blob](https://github.com/google/go-containerregistry/blob/main/cmd/crane/doc/crane_blob.md) | `manifest IMAGE`, `blob BLOB` with a combined repository/digest reference | Supports considering canonical positional references in #88, not broadening this item. |
| [regctl artifact get](https://regclient.org/cli/regctl/artifact/get/) / [manifest get](https://regclient.org/cli/regctl/manifest/get/) / [blob get](https://regclient.org/cli/regctl/blob/get/) | Artifact and manifest targets positional; blobs take repository plus digest | Artifact vocabulary is useful, but there is no universal spelling to copy. |

The selected `--artifact` flag follows the owner's preference and the current
flag-based RegShape interface. It is not presented as an ecosystem standard.

### 7. Desired experience and command matrix

Every listed command accepts all four spellings of the same target option.

| Command | Metavariable | Unchanged target semantics |
| --- | --- | --- |
| `manifest get` | `ARTIFACT` | Registry/repository plus tag or digest; existing default tag behavior. |
| `manifest info` | `ARTIFACT` | Same target rules; metadata-only request remains unchanged. |
| `manifest descriptor` | `ARTIFACT` | Same target rules; descriptor result remains unchanged. |
| `manifest put` | `ARTIFACT` | Existing target/input rules; payload still supplied by file/stdin. |
| `manifest delete` | `ARTIFACT` | Digest-only; no tag dereferencing added. |
| `tag list` | `REPOSITORY` | Repository namespace; existing tag/digest suffix ignoring remains explicit. |
| `tag delete` | `ARTIFACT` | Existing tag semantics and digest rejection. |
| `referrer list` | `ARTIFACT` | Existing tag-to-digest resolution and default latest behavior. |

The command determines what is operated on: `--artifact` selects the reference,
not an instruction to fetch the complete content graph. `--artifact-type`
remains an independent referrer filter. No command group is renamed.

Keep Docker export/push `--image / -i` and layout layer `--index / -i`.
Layout/Docker `--dest` remains named `--dest`; update only misleading destination
help/metavariables where the operation is registry-oriented. Keep actual
image-specific restrictions, OCI Image Manifest/Image Index type names, and
platform fields accurate.

Python callers get the additive parser entry point in Part II. There is no
graphical interface or infrastructure-template surface. Packaging stays the
existing Python package/console entry point; no new install steps are required.

### 7A. Proposed user-facing output

These are future-interface samples, not commands supported by the baseline.
The tag results below come from a synthetic repository containing `v1` and `v2`.

Preferred invocation:

```console
$ regshape tag list --artifact registry.example.com/team/package
v1
v2
```

Legacy invocation, still supported with the same result and no warning:

```console
$ regshape tag list -i registry.example.com/team/package
v1
v2
```

Help excerpt (Click controls wrapping and alias ordering):

```text
-a, -i, --artifact, --image-ref ARTIFACT
    Artifact reference with embedded registry.
    Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a.
    Retained for compatibility. [required]
```

Conflicting values (stderr, exit 2; no result on stdout):

```console
$ regshape manifest get --artifact registry.example.com/team/package:v1 -i registry.example.com/team/package:v2
Usage: regshape manifest get [OPTIONS]
Try 'regshape manifest get --help' for help.

Error: Conflicting reference options. Supply one artifact reference.
```

The rejection also applies to conflicting repetitions of just one spelling.
An identical repeated value is accepted and resolved once. The error deliberately
does not echo supplied reference values.

### 8. Success metrics and phase gates

The outcome is clearer naming without breaking valid single-target workflows.
Measure delivery through deterministic checks, not inferred adoption.

| Signal | Baseline | Target / gate |
| --- | --- | --- |
| Affected commands accepting the preferred aliases | 0 of 8 | 8 of 8 before command-integration merge. |
| Legacy aliases retained | 2 per affected command | All 16 retained through 0.2 delivery. |
| Positive command/spelling coverage | No artifact-alias cases | At least 32 combinations (8 commands x 4 spellings), plus operation-specific tests. |
| Added credential/network actions from alias resolution | 0 | 0; conflict/missing input also performs no credential/registry calls. |
| Additional runtime deprecation output | 0 | 0; stdout/results and existing telemetry stay unchanged. |
| Hosted runtime coverage | Python 3.10, 3.11, 3.12 | All three pass at each implementation head before merge. |
| User migration documentation | No preferred artifact flag | Validated help/examples and required docs child complete before close-out. |

Counts are feature acceptance criteria, not projected customer usage.
The phase gates are design approval, shared-support validation, command matrix
validation, documentation review, then inventory close-out. No release date is
promised. Maintain the existing suite rather than accepting a reduced baseline.

### 9. Pricing

No pricing/billing change: this is an additive local CLI/library interface.

### 10. Performance and scale

For N explicit target occurrences of maximum length L, duplicate comparison
must be O(N x L) time; the resolver should add only constant auxiliary state
beyond Click's captured values. Ordinary commands normally provide one target.
The shell/operating system bounds the total command line; this feature adds no
new registry quotas, network calls, payload buffers or background processes.

Test ordinary input, all four aliases, a long reference, and 1,000 identical
occurrences in a synthetic Click invocation. Confirm one downstream operation
and no callback growth beyond captured values. Record local parsing timings if
investigating a regression, but do not claim unmeasured latency percentiles or
a registry availability objective for local option parsing.

> NOTE: No production adoption, throughput or latency-percentile baseline was
> supplied. Network/service-level performance promises are not part of this
> naming change.

### 11. Observability and troubleshooting

| Symptom | Diagnose | Recovery |
| --- | --- | --- |
| `No such option: --artifact` | Installed executable predates the feature; inspect command help. | Use the compatible old flag or install a release containing the new option. |
| Conflict usage error | More than one distinct target value was supplied. | Keep one value, or make repeated spellings identical. |
| Missing target usage error | None of the four aliases supplied a value. | Supply `--artifact VALUE`; Click retains standard usage errors. |
| Legacy name in a script | Supported, but deprecated in help/docs. | Replace with `--artifact` or `-a` when convenient; no immediate removal. |
| Registry/authentication failure | Option parsing succeeded; existing transport diagnostics apply. | Follow existing registry/credential guidance; aliases do not refresh tokens. |

No new telemetry flags, remote usage tracking or secret-bearing diagnostics are
added. Local parsing errors use Click's stderr/exit-code channel; operation
errors retain their current repository-standard handling.

### 12. Security

The boundary remains shell input -> local parsing -> existing library/transport
-> user-selected registry. Alias conflict checks happen before credential
lookup or network access. No credential/configuration storage, scope, token,
TLS, redirect, payload or permission changes are authorized.

Do not interpolate references into shell commands, add new URL interpretation,
or log supplied conflicting values. Existing public/private registry choices
and secure defaults remain unchanged. No new secret, service identity or
network listener is introduced.

### 13. Risks

The implementer owns mitigation of alias/deprecation mistakes and parser
regressions; the documentation work item owns migration clarity. Ambiguous
duplicates intentionally change behavior, and must be disclosed. Broader
reference redesign might later prefer positional arguments; keep this
compatibility surface small rather than implementing that redesign prematurely.
Detailed mitigations are in Part II E/F/M.

### 14. Delivery and release plan

1. Owner reviews this complete design and native work breakdown.
2. With explicit merge authorization and passing review/checks, merge the
   design/inventory prerequisite; the inventory says In progress, not delivered.
3. Implement and review shared support, then command integration, one child
   per pull request (PR).
4. Merge the final documentation item only when it matches the actual command
   help and parser signatures.
5. Merge a separately authorized inventory close-out before closing #87.
6. Release preparation/publication is a separate owner action; 0.2 is the target,
   not a claimed published version.

There are no hosted-service preview regions or customer cohorts for this local
interface. The package can be adopted by upgrading; scripts using old flags
continue to work under the compatibility constraints below.

> NOTE: No dated launch or removal milestone was supplied. Do not invent a
> version after 0.2 at which the old aliases disappear.

## Part II: Engineering design

### A. Abstract

This design adds an artifact-oriented selector to eight existing registry
commands while preserving their operation contracts. A small shared Click
helper declares four spellings for one required target, collects explicit
occurrences, and resolves them to the existing single string parameter.
Identical duplicates are accepted; differing values cause a usage error before
credentials or registry requests. Help identifies only the old image-reference
spellings as deprecated in 0.2, without adding runtime warnings. A new
artifact-named Python parser entry point delegates to the existing parser so
that old positional and keyword callers retain their grammar, return values
and errors. Command integration changes terminology, not payload handling,
authentication, output schemas or supported artifact formats. Docker's local
image selectors and unrelated uses of the same short flag remain untouched.
Three delivery items separate shared compatibility support, command wiring
and regression tests, and final user documentation. Hosted checks and agent
reviews validate each item; this design PR remains unmerged until the owner
approves the complete specification and breakdown.

### B. Prerequisites

Requirements are recorded in [#87](https://github.com/toddysm/regshape/issues/87)
and Part I. The owner selected `--artifact / -a`, requested a 0.2 deprecation
notice and authorized hosted Actions/agent-only reviews. This does not approve
unreviewed design details, code merges or releases. No implementation may start
until the design approval/inventory prerequisite is satisfied.

### C. Introduction

The baseline already supports tag/digest references through image-named options.
The needed change is a compatibility layer and accurate language across the
existing eight commands, ahead of the separately planned artifact/reference
features. A new parser grammar or new content model is neither needed nor safe
to introduce as a naming change.

### D. Components and flow

```text
--artifact / -a / --image-ref / -i
                 |
                 v
  shared Click declaration + duplicate resolver (new)
                 |             |
       one image_ref string    +--> usage error, exit 2
                 v                  (no credential/network calls)
  manifest / tag / referrer command integration (changed)
                 |
                 v
  parse_artifact_ref (new name) -> parse_image_ref (existing semantics)
                 |
                 v
  existing registry client, operation, formatter, telemetry/progress
                          (behavior unchanged)
```

#### D1. Shared Click helper

The shared [artifact_option](../../src/regshape/cli/options.py) decorator has
the signature `artifact_option(*, help_text: str, metavar: str = "ARTIFACT")`.
It accepts operation-specific help and an optional metavar (default `ARTIFACT`;
tag list uses `REPOSITORY`). It declares `--artifact`, `-a`, `--image-ref`, `-i`
with the explicit destination name `image_ref`, `required=True`, text values,
and public Click support for collecting repeated values.

Use `multiple=True` plus a shared option callback to retain explicit occurrences
instead of accepting Click's last-value-wins behavior. After Click handles
missing required input, the callback compares each value with the first:
identical values produce that one string; a mismatch raises `click.UsageError`
with the exact error in section 7A. No reference normalization, credential
lookup, digest resolution or network equivalence check happens here.

The callback must tolerate Click's resilient help/completion parsing without
inventing a valid target or invoking command code. Do not couple the helper to
one command or add a new parser framework/private Click hooks. Tests should
verify the public behavior, including `--artifact=value`, `-avalue`, normal
separate values and legacy equivalents.

Append an explicit help notice naming only the legacy aliases:
`Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a. Retained for compatibility.`
Do not set option-wide `deprecated=True`, which would wrongly apply to the
preferred aliases and can generate runtime output.

#### D2. Parser API

Add `parse_artifact_ref(artifact_ref: str) -> tuple[str, str, str]` to
[libs/refs.py](../../src/regshape/libs/refs.py). Delegate to the existing
`parse_image_ref` implementation. Preserve the old function name, `image_ref`
keyword, tuple values, defaults and `ValueError` behavior. `format_ref` stays
unchanged. This is additive in the 0.2 package interface; no network API,
persisted schema, cache key or data migration is introduced.

#### D3. Command integration

Use the shared decorator and neutral parser entry point in the affected
[manifest](../../src/regshape/cli/manifest.py),
[tag](../../src/regshape/cli/tag.py), and
[referrer](../../src/regshape/cli/referrer.py) modules. Retain the internal
`image_ref` callback parameter where needed for compatibility; internal variable
names do not dictate user-facing help.

Update relevant [layout](../../src/regshape/cli/layout.py) and
[Docker](../../src/regshape/cli/docker.py) destination wording without changing
destination flags, accepted target types, or Docker source selectors.
Existing image-model/specification names are not renamed.

New values live only for one command invocation. Shared resolver state is not
stored globally or reused across commands. Existing decorator ordering must
retain eager help and ensure usage errors precede command-body side effects.

### PM-1. Requirements traceability

| Requirement/scenario | Design element | Validation |
| --- | --- | --- |
| R1, S1 | D1/D3 and command matrix | 32 positive command/spelling cases; operation arguments/results equivalent. |
| R2, S2 | D1 legacy-specific notice | Help tests for all aliases; no runtime warning; old calls still work. |
| R3, S3 | D1 duplicate resolution | Same/different values, spellings and orders; exit 2; no credential/network calls. |
| R4 | D3 unchanged operation paths | Command-specific regression tests and unchanged output/request assertions. |
| R5, S4 | D2 additive parser API | Positional/keyword old callers and new API return/error equivalence. |
| R6 | D3 scope exclusions | Docker/image/index options remain valid and lack the deprecation notice. |
| R7 | Delivery docs item | Help/signature/example/link validation and reviewed migration guide. |

There are no untracked requirement deferrals or unexplained extra components.
Cloud-product evidence gaps are named in Part I, not filled with invented data.

### PM-2. Public contracts and compatibility

Preferred flags and Python entry point are additive. No remote endpoints,
request bodies, credential formats or result schemas change. Existing mutating
operations retain their own idempotency/retry/permission behavior.

Compatibility covers existing valid single-target calls. Deliberately changing
ambiguous repeated options from last-value-wins to exit 2 is a user-visible
behavior change, including repetitions of legacy spellings; disclose it in
release/migration notes and obtain approval with this design.

### PM-3. Measurement and cost

Part I metric targets map to parameterized pytest assertions and hosted check
results persisted per commit in GitHub. Existing optional request/scenario
telemetry remains unchanged; do not claim it distinguishes which alias was used.
The additional runtime work is bounded local argument comparison, with zero
added remote requests/storage. Development cost consists of normal existing
hosted test/review runs, visible in Actions usage; no new service is deployed.

> NOTE: RegShape has no customer-usage backend for this feature. Longitudinal
> per-customer adoption/churn/frequency measurement is not available. Adding a
> telemetry service merely to satisfy a cloud-product review criterion would
> be out of scope and a privacy change; that review criterion remains an
> explicit evidence gap, not an asserted pass.

### PM-4. Authentication, authorization and networking

Registry credentials are still selected by the existing resolver, including
configured helpers. Registry permissions/scopes are unchanged; no Azure role,
service identity, credential, firewall, private endpoint or egress rule is
created or changed. Secure registry transport defaults and `--insecure` behavior
remain intact. Alias errors run before those components are entered.

### PM-5. Error and edge-case contract

| Input/failure | Behavior |
| --- | --- |
| No alias/value | Existing Click usage error, exit 2. |
| Identical explicit values, including repeated legacy spelling | Resolve once; normal operation. |
| Any different explicit value | Exact generic conflict error, exit 2; no credentials/network. |
| Explicit empty or malformed reference | Pass a single value into existing parsing/command checks; preserve their errors. |
| Tag/digest unsupported by the selected operation | Existing operation-specific restriction/error. |
| Help or resilient completion | No registry/credential side effects; no false target. |
| Transport, auth, parsing, output or cancellation failure after selection | Existing operation behavior; no new catches/fallbacks. |

Comparison is raw-string equality: a tag and a digest that happen to name the
same remote object are still distinct arguments. Do not fetch to decide
equivalence. Do not add independent retry or partial-file handling for aliases.

### E. Risk identification and assessment

| Area | Risk assessment | Mitigation |
| --- | --- | --- |
| Breaking changes | Conflicting duplicates no longer choose the last value. | Explicit PM-2/F contract and tests; migration note. |
| Security/auth | Resolver could run after credentials/network activity. | Click callback boundary; mock constructors/credential lookup and assert not called. |
| Privacy | Error might disclose supplied references or add usage telemetry. | Generic conflict error; no reference logging or usage backend. |
| Dependencies | Option-wide deprecation/version-specific Click behavior. | Public Click APIs, explicit legacy-only help; supported dependency/runtime checks. |
| Infrastructure/buildout | None added; ordinary package rollback still matters. | No configuration migration; retain old interface. |
| Multi-tenancy/contention | No shared service; accidental global resolver state possible. | Invocation-local values; repeat-invocation tests. |
| Regression: shared parser | Rename could break imports/keyword callers. | Additive wrapper and old/new equivalence tests. |
| Regression: command modules | Mutating command could target the wrong object. | Eight-command matrix and conflicts rejected before operation calls. |
| Regression: unrelated commands | Broad replacement could rename Docker/local index flags. | Explicit excluded-surface tests and scoped edits. |
| Documentation | Claims might imply arbitrary new media-type support. | Keep existing support boundaries and link separate feature work. |

### F. Breaking changes and deprecation

No flag or Python API is removed in 0.2. The only intentional behavior change
is explicit conflict rejection for differing duplicate target values. Help,
metavariables and documentation change as specified. Deprecation begins in 0.2,
not an unspecified version afterward; a removal release requires a separate
owner decision. Keep raw/JSON/table/list payloads unchanged.

### G. Security risk management

No new execution or credential boundary is needed. The resolver compares text,
does not evaluate it, and does not broaden parser syntax or registry routing.
Keep generic errors free of supplied target values; use synthetic fixtures.
No new cryptography, keys, dependencies or secret migration is introduced.
Use scoped security/lint comparisons to distinguish new findings from legacy
debt rather than silently expanding this feature.

### H. Privacy and data sovereignty

No new collection, persistence, retention or transfer of user information.
Existing registry communication remains controlled by existing inputs/config.
Do not add per-customer tracking or output supplied credentials/references in
the new error path. Regional service deployment is not applicable.

### I. Observability risk management

Help communicates the migration; exit 2 and a specific error identify conflicts.
Existing operational telemetry continues to describe the actual registry work,
not a new alias-specific metric. Hosted test results demonstrate compatibility,
not installed-customer adoption. Use the symptom table in Part I for triage.

### J. Dependencies

No new runtime dependency or upgrade. Current requirements are Python >=3.10
and Click >=8.1. Use public Click option/callback behavior; do not assume the
latest-only deprecation API. The existing hosted matrix covers Python
3.10/3.11/3.12; validate the declared minimum Click behavior when exercising the
new helper rather than silently raising the minimum.

### K. Deployment and buildout

Land design/inventory first after approval; shared support next; command wiring
next; final documentation and inventory close-out last. Existing automated
package/release workflows remain unchanged and publishing is a separate owner
action. No backfill, persistent migration, region rollout or manual secret
change is required. No runtime feature flag is needed for additive aliases.

### L. Multi-tenancy

Not applicable: this is a local CLI/library interface, not a multi-tenant
service. Resolver state must nevertheless remain invocation-local.

### M. Regression coverage

Cover [reference tests](../../src/regshape/tests/test_refs.py),
[manifest commands](../../src/regshape/tests/test_manifest_cli.py),
[tag commands](../../src/regshape/tests/test_tag_cli.py), and
[referrer commands](../../src/regshape/tests/test_referrer_cli.py), plus
unaffected [Docker](../../src/regshape/tests/test_docker_cli.py),
[layout](../../src/regshape/tests/test_layout_cli.py) and
[blob](../../src/regshape/tests/test_blob_cli.py) surfaces. Add a dedicated
shared-option test module in the implementation item.

Validate byte-for-byte result equivalence for equivalent command invocations,
unchanged operation arguments and no extra network calls. Preserve existing
tests on legacy aliases rather than mass-renaming them to hide regressions.
Source documentation must cover new/changed declarations and test helpers.

### N. Rollback

Before release, revert implementation PRs through ordinary review if needed;
there is no persisted migration to undo. Users can return to `--image-ref/-i`,
which remains supported in the new version and works on the previous version.
If a user adopts `parse_artifact_ref`, a package downgrade requires returning to
`parse_image_ref`; do not claim the new API exists in old distributions.
No automatic downgrade, destructive git action, or release rollback is authorized.

### PM-6. Phasing and effort estimates

These are rough engineering estimates in person-days, not delivery-date promises
or measured agent runtime. Assumptions: current tests remain green, no parser
grammar change, and no new dependency. Review/check latency is variable.

| Component/work item | Implement/write | Validate/test | Review/integration |
| --- | --- | --- | --- |
| Shared Click helper and parser wrapper, #93 | 0.5 | 0.5 | 0.25 |
| Three command modules plus scoped destination wording, #94 | 0.5 | 0.75 | 0.25 |
| User/developer migration docs, #95 | 0.5 | 0.25 | 0.25 |
| Inventory close-out and rollback/readiness check | 0.25 | 0.25 | 0.25 |

Main risks are Click help/repeat behavior, preserving old keyword/default
callers, and accidentally widening the command migration. No cross-team
service rollout is required. The scope remains one small PR per delivery item.

### PM-7. Test plan and continuous integration

1. Unit-test old/new parser outputs/errors and existing keyword compatibility.
2. Test the shared helper using a synthetic command: four spellings,
   attached/equals forms, same/different repeats, missing values, eager help,
   resilient parsing, and a 1,000-occurrence case.
3. Parameterize the 8 x 4 command/spelling matrix with mocked libraries;
   verify actual arguments, results and operation-specific restrictions.
4. Assert usage failures never construct the registry client or resolve
   credentials; verify existing options, file/stdin handling and output modes.
5. Check excluded Docker/layout/blob flags and genuine image terminology.
6. Run targeted tests first, scoped type/lint/security checks, and existing
   [hosted tests](../../.github/workflows/tests.yml) on every implementation PR.
   The workflow installs `.[dev]` and runs `python -m pytest -q` on
   Python 3.10/3.11/3.12. A focused `click-minimum` job also installs
   `click==8.1.0` on Python 3.10 and runs the shared-option and reference tests,
   retaining the declared dependency floor. All required checks/reviews remain binding.
7. Documentation-only validation checks relative links/anchors, help and
   signatures, examples and whitespace. No docs builder/linter is currently
   configured; do not claim an unavailable build was run.

All new validation can use synthetic inputs without real credentials, registry
access or a Docker daemon. Mocked tests are not live-registry certification.
The design experiment proved alias/conflict behavior with Click 8.3.1.
Shared-support tests now exercise the implementation with both Click 8.1.0
and 8.3.1 locally; the targeted hosted job covers the minimum dependency on
Python 3.10. This foundation is not production command wiring.

### O. Next actions and native work breakdown

| Item | Depends on | Planned branch | Current delivery state |
| --- | --- | --- | --- |
| [#92: design and inventory](https://github.com/toddysm/regshape/issues/92) | Owner approval before merge | `prd/artifact-neutral-cli-fd2ca782` | Done: #96 merged |
| [#93: shared support](https://github.com/toddysm/regshape/issues/93) | #92 approved/merged | `feature/artifact-neutral-cli/shared-support-fd2ca782` | In progress |
| [#94: command integration](https://github.com/toddysm/regshape/issues/94) | #93 | `feature/artifact-neutral-cli/command-aliases-fd2ca782` | Blocked |
| [#95: final user docs](https://github.com/toddysm/regshape/issues/95) | #93 and #94 | `feature/artifact-neutral-cli/migration-docs-fd2ca782` | Blocked |

The feature remains open until all required children and the inventory
close-out are merged. No external partner/legal launch is required for this
local naming change; owner approval and documentation/release-note readiness
are the launch checklist. Do not begin #91/#88/#90 as part of this delivery.

### P. References

- [Feature decisions and constraints](https://github.com/toddysm/regshape/issues/87)
- [Capability inventory](../../docs/project-state.md)
- [Manifest reference](manifest.md), [tag reference](tag.md),
  [referrer reference](referrer.md)
- [Package/dependency contract](../../pyproject.toml)
- [Hosted test workflow](../../.github/workflows/tests.yml)
- Existing progress behavior: [user guide](../../docs/guides/live-transfer-progress.md)
  and [library contract](../../docs/library/transfer-progress.md)
