# Manifest GET progress: functional requirements and engineering design

- **Status:** Approved revision 3 and breakdown; library item #105 implementation authorized.
- **Feature:** [#90](https://github.com/toddysm/regshape/issues/90).
- **Design item:** [#101](https://github.com/toddysm/regshape/issues/101).
- **Target:** [0.2](https://github.com/toddysm/regshape/milestone/1); not a release commitment.
- **Author:** RegShape owner requirements, developed with the coding assistant.
- **Date / inspected baseline:** 2026-11-04 / `662216f3ff508e83598c9f8af2096e50c682e20c`.
- **Authorization:** Design artifacts, hosted GitHub Actions, agent-only reviews
  and merging [design PR #106](https://github.com/toddysm/regshape/pull/106).
  The inventory-start transition and library item #105 through an open reviewed
  implementation PR are authorized. Its merge, command integration, final user
  documentation delivery, release and branch/worktree cleanup are not authorized.

This combined product requirements document (PRD) and engineering design document
(EDD) specifies proposed behavior, not a feature already available on main.
Command-line interface (CLI), application programming interface (API), Open
Container Initiative (OCI), and continuous integration (CI) are abbreviated below.

## Part I: functional requirements

### 1. Overview

Give registry users visible, truthful download progress for a single manifest or
manifest index, without damaging their command output. Extend the existing shared
progress display rather than introducing a second display system. Provide optional
Python observations that future full-artifact downloads can reuse.

### 2. Problem and evidence

The owner successfully exercised blob progress, then explicitly requested:
"manifest GET doesn't have progress bar, we need to add that too." The owner
selected this feature before [full-artifact download](https://github.com/toddysm/regshape/issues/91)
and [canonical reference standardization](https://github.com/toddysm/regshape/issues/88).

At the inspected baseline, [get_manifest](../../src/regshape/libs/manifests/operations.py)
returns buffered response text without observations, and
[manifest get](../../src/regshape/cli/manifest.py) has no progress context.
The existing [shared renderer](../../src/regshape/cli/progress.py) handles blob
events, not manifests. This is a demonstrated coverage gap, not evidence that
existing blob progress is broken.

> NOTE: Evidence is the owner's request and inspected implementation. Three
> named customers, adoption analytics and quantified support-volume evidence are
> unavailable; this proposal makes no market-wide or customer-count claims.

### 3. Scenarios

- **S1 - Interactive inspection.** Todd, the registry operator who requested the
  feature, fetches a large tagged index on a slow connection. Today the command
  appears idle until the body arrives. With this change, one stderr row advances
  while bytes arrive, becomes Processing and Writing, and finishes Downloaded
  after the requested result has been emitted. A digest target follows the same
  path without a preliminary metadata request.
- **S2 - Raw export and automation.** The same operator saves decoded raw
  manifest text with `--raw --output`, or pipes normal JSON to another program.
  Terminal stderr can show progress while stdout/file data remains unchanged.
  Nonterminal stderr or `--no-progress` suppresses animation. A missing length or
  compressed response shows bytes and an unknown total, not a fake percentage.
- **S3 - Post-download failure and interruption.** The operator requests a
  nonexistent subject, encounters malformed manifest JSON, or cannot finish
  writing the output. Receipt alone must not leave a Downloaded row: the same
  row becomes Failed, followed by the existing error behavior. An interrupt
  becomes Cancelled; partially written output is not presented as transactional.
- **S4 - Library orchestration.** An application author using the same registry
  workflow opts into observations while fetching several objects sequentially.
  A transfer can start before its digest or media type is known. A stable opaque
  identity allows a caller to track it without fabricating a digest; existing
  callers that omit observations retain their tuple result and buffered behavior.

S2-S4 are design scenarios derived from the request and existing options, not
claims about additional measured customers.

### 4. Outcomes, priorities and scope

The required outcome is confidence that work is advancing without false success,
extra network probes or corrupted result data.

| Requirement | Priority | Acceptance boundary |
| --- | --- | --- |
| R1 | Must | Optional library observations report bytes during consumption and preserve the existing return contract. |
| R2 | Must | Stable manifest identity works without a digest; blob event invariants remain unchanged. |
| R3 | Must | One manifest/index row updates in place through transfer and command terminal states. |
| R4 | Must | Downloaded follows required parsing, extraction and output, not merely receipt. |
| R5 | Must | Existing options, aliases, output formatting, decoding, errors and auth behavior remain compatible. |
| R6 | Must | Totals, percentage, rate and estimated remaining time are honest; no progress-only network requests. |
| R7 | Must | Existing suppression, redraw limits, terminal sizing and diagnostic coordination are reused. |
| R8 | Must | Response ownership, streamed cache/retry safety, cancellation and primary-exception preservation are tested. |
| R9 | Must | Deterministic library, command and visible-frame tests protect new and existing progress. |
| R10 | Must | Final user/API documentation and capability inventory accurately describe availability. |
| R11 | Should | The event/display path can be reused by sequential full-artifact orchestration without a second renderer. |

In scope: manifest GET observations, the shared display extension, command wiring,
narrow stream-safety fixes to optional transport caching/retries, and documentation.
Not in scope: graph traversal or blob fetching
([#91](https://github.com/toddysm/regshape/issues/91)); new reference grammar
([#88](https://github.com/toddysm/regshape/issues/88)); credential-setup documentation
([#86](https://github.com/toddysm/regshape/issues/86)); automatic credential
refresh; manifest upload progress; parallel transfers; retry/resume of a partially
read manifest; new digest validation; binary export; a new telemetry backend.
These are scope boundaries, not claims that the adjacent work is implemented.

### 5. Existing solution and reuse

[Live blob progress](../../docs/guides/live-transfer-progress.md) already supplies
stderr rendering, suppression, throttling, width handling and diagnostic cleanup.
Extend it. Its [TransferProgress](../../src/regshape/libs/progress.py) requires a
valid blob digest and its completed phase implies successful verification; do not
relax that contract to fit tag-based manifest transfers.

### 6. External alternatives

No competitive progress-parity claim is needed for this extension. The earlier
[artifact-option design](artifact-options.md) records the naming decision; this
work keeps those aliases rather than reopening the naming comparison.

### 7. User experience

Proposed command, with the existing default media-type negotiation:

```bash
regshape manifest get --artifact registry.example.invalid/team/package:v1
regshape manifest get --artifact registry.example.invalid/team/package:v1 \
  --raw --output manifest.json
regshape manifest get --artifact registry.example.invalid/team/package:v1 \
  --part annotations --no-progress
```

The example registry is deliberately non-routable; substitute a registry the
caller can access. Authenticate separately using the existing auth command.
`--artifact/-a` is preferred; `--image-ref/-i` remains the deprecated-in-0.2
compatible alias. `--accept`, `--raw`, `--part` and `--output/-o` retain their
meaning. `--raw` and `--part` remain mutually exclusive before network activity.

Add `--no-progress` to this command only, with the same help/semantics as blob
commands. Rendering defaults to interactive stderr. Nonterminal stderr, explicit
opt-out and structured telemetry suppress animation. Normal manifest JSON on
stdout is result data, not a structured-telemetry suppression signal. No separate
structured-output flag is invented.

Python users opt in via a callback; no terminal dependencies enter the library.
Graphical interfaces, infrastructure-as-code templates and hosted service
deployment are not applicable: this is a local library/CLI extension.

### 7A. Output samples

These are proposed visible frames at a sufficiently wide terminal, not captured
current behavior. Each separate block is a snapshot of the **same overwritten
stderr row**, not a new line appended for each phase. Byte units and adaptive
truncation reuse the existing renderer. No reference or digest is required in
the initial label.
When emitting the result to stdout, temporarily clear/suspend that row and restore
one row after the payload. Its logical identity stays the same, but its physical
terminal position can move; never retain an old Writing row beside the result.

Known-size index, during transfer (illustrative measured rate):

```text
Index 1 Downloading 64.0 KiB/256.0 KiB [###---------] 25% 32.0 KiB/s ~6s remaining
```

After receipt, before output completion:

```text
Index 1 Processing 256.0 KiB/256.0 KiB
```

```text
Index 1 Writing 256.0 KiB/256.0 KiB
```

Success, flushed once:

```text
Index 1 Downloaded 256.0 KiB/256.0 KiB
```

Unknown total; no percentage or estimated remaining time:

```text
Manifest 1 Downloading 64.0 KiB (total unknown) 32.0 KiB/s
```

Missing subject after receipt; the standard error follows row cleanup, stdout
is empty, and the command exits 2:

```text
Manifest 1 Failed 2.0 KiB/2.0 KiB
Error [registry.example.invalid/team/package:v1]: manifest has no subject field
```

Interrupt while receiving:

```text
Manifest 1 Cancelled 64.0 KiB/256.0 KiB
```

The existing Click interruption diagnostic/exit behavior follows cleanup.
An output failure may already have written some data: the row becomes Failed,
but this feature does not erase stdout or roll back an output file. A successful
`--raw` invocation is still decoded text plus the existing newline behavior,
not proof that the payload is a valid manifest or matches a digest.

### 8. Success metrics and gates

| Outcome | Baseline | Target / measurement | Deadline |
| --- | --- | --- | --- |
| Incremental visibility | No manifest observations | A streamed 256 KiB fixture exposes intermediate 64 KiB consumption before EOF | Library delivery gate |
| Stable identity | Blob-only display | Exactly one visible row for a transfer, including kind discovery and all final states | Command delivery gate |
| Truthful completion | No manifest row | Zero Downloaded frames before required output succeeds; failures overwrite the same row | Command delivery gate |
| Output compatibility | Existing manifest fixtures | Identical result text/files and existing expected exit codes across progress modes | Command delivery gate |
| Resource/network guardrails | Current transport policy | Owned responses closed on every path; zero extra progress-only HEAD/GET calls | Library delivery gate |
| Shared-renderer guardrails | At most 10 routine redraws/second | Same bound, with forced lifecycle/final updates and unchanged blob regressions | Command delivery gate |
| Documentation readiness | Guide excludes manifests | Implemented examples, callback reference and links verified | Documentation gate |

These are deterministic acceptance thresholds, not measured production adoption.
There is no background usage collection. The owner can validate the experience
against the examples before release; no release date or adoption target is known.
The no-extra-probe guardrail is per logical fetch, not a promise of identical
cache-hit counts: observed fetches bypass the optional live-response cache as
specified in D3.1.

### 9. Pricing

No pricing, billing meter or monetization change.

### 10. Performance and scale

Observed transfers consume nonempty byte chunks using an internal 64 KiB request
size, like the existing transfer work. Larger bodies produce intermediate events;
a small manifest can finish in one read without an artificial sleep. Requests may
yield chunks smaller than the requested size. Callbacks are synchronous and can
slow consumption; document that observers should return promptly.

The result remains a complete string. Collecting bytes and decoding therefore
remains O(body size) memory, not a bounded-memory manifest API. Avoid repeated
string concatenation, disk spooling or an extra fetch. There is no new manifest
size cap, parallelism, quota, timeout or automatic partial-body retry.

> NOTE: No production p50/p95/p99 latency, availability or peak-size measurements
> were supplied. No service-level promise is made. Deterministic chunk/read/redraw
> thresholds above are the release gate; synthetic tests include empty, small
> and multi-chunk bodies. Very large complete results retain the existing
> memory-exhaustion risk, rather than a new unsupported bounded-memory claim.

### 11. Observability and troubleshooting

Bytes, phase and optional totals tell the caller whether body consumption is
advancing. Unknown length is not failure. HTTP/authentication errors retain
their normal diagnostics; reauthenticate an expired credential separately.
Parsing or missing-part failure is distinguished from successful receipt.
Interrupted or failed output may need removal/retry by the caller.

An observed request uses the existing **streaming** diagnostic behavior:
request timing ends at headers, while operation/scenario timing includes body
consumption. Request debug does not pre-read the body. Its existing JSON fields
`body_preview: null` and `body_size: 0` denote no captured body at that layer,
not a zero-byte manifest. The text formatter's existing `(empty)` placeholder
likewise describes the absent preview, not payload validity or size. Document
this limitation explicitly; progress bytes are the consumption signal.
This intentionally differs from the old buffered manifest body preview, while
preserving the telemetry schema. No unrelated telemetry-coverage expansion or
preview-capture mechanism is part of this design.

### 12. Security

Keep the existing registry client, authentication challenge flow and repository
pull scope. No added permissions, credential access, listener or external service.
Normal transport encryption and the existing explicit insecure option are unchanged.
Do not include credentials, response bodies, signed URLs or headers in progress
events. Terminal labels are fixed Manifest/Index words and ordinal numbers.
Callback consumers remain responsible for their own handling of observed data.

### 13. Main risks

False success after receipt, charset drift, untrusted length metadata, leaked
responses and shared-renderer regressions are the principal technical risks.
The library and command work-item owners mitigate them through the contracts
and failure tests below; the documentation owner verifies the user-facing limits.

### 14. Rollout

Design review and explicit owner approval precede delivery. Then merge the
library slice, command slice and final documentation separately, with hosted
checks and agent-only reviews at each gate. A separately authorized inventory
close-out records Implemented only after delivery. Milestone 0.2 is the target;
version bump, packaging/publication and release announcement require their own
authorization. There is no regional rollout or cloud backfill.

## Part II: engineering design

### A. Abstract

This design extends RegShape's existing live transfer display to manifest
downloads while preserving its library and command result contracts. An optional
callback on the manifest fetch operation enables incremental response consumption
and emits immutable observations identified by an opaque transfer identifier.
The event does not require or fabricate a digest, and its received state means
only that the library has successfully consumed, decoded and closed the response.
The shared terminal display accepts this event alongside unchanged blob events,
keeps one row throughout the operation, and delays the Downloaded state until the
command has completed any required parsing, field extraction and output writing.
Unknown lengths remain unknown; inconsistent metadata cannot produce an impossible
percentage. Existing Requests text decoding, authentication, output formatting,
diagnostic coordination and cancellation behavior are preserved. The design adds
no network probe, package dependency, binary-export mode or manifest integrity
guarantee. Focused library and terminal-frame tests, followed by the existing
hosted checks and final user documentation, provide the delivery gates.

### B. Prerequisites and approval

The owner approved this complete revision and breakdown under
[#90](https://github.com/toddysm/regshape/issues/90), including merging
[design PR #106](https://github.com/toddysm/regshape/pull/106).
Library item #105 is now authorized through an open reviewed implementation PR.
Command integration and final user documentation require later authorization. Shared progress
([#72](https://github.com/toddysm/regshape/issues/72)) and artifact aliases
([#87](https://github.com/toddysm/regshape/issues/87)) are merged prerequisites.
No hard dependency on #91 or #88 is introduced.

### C. Current gap

The manifest operation reads `response.text` after a nonstreaming request.
The command parses/extracts/writes only after that return. The current renderer
keys rows by blob digest and finalizes a blob completion event immediately.
None of those facts permits simply relabeling a blob event as a manifest:
tag identity, absent digest headers and later command failures must be handled.

### D. Chosen design

#### D1. Components and flow

```text
manifest get
  | existing reference/options/credential setup
  | enter shared ProgressDisplay context
  v
get_manifest(progress_callback=display.observe)
  | existing RegistryClient GET, stream=True
  | headers -> classification/optional length
  | iter_content -> absolute byte observations -> shared display
  | collected bytes -> Requests-compatible text decoder
  | response close -> RECEIVED observation
  v
optional parse/extract -> Processing -> Writing
  | stdout: clear/suspend row -> existing output helper -> rebase row
  | file: existing output helper without terminal handoff
  | success: finish the same row as Downloaded
  | error/interrupt: unwind row as Failed/Cancelled, preserve original error
  v
existing command result/error handling; no progress on stdout
```

| Component | Planned location | Responsibilities / requirements |
| --- | --- | --- |
| Additive manifest event types | [Shared library progress module](../../src/regshape/libs/progress.py) | R1, R2, R8: independent manifest contract and callback type |
| Observed fetch / private text adapter | [Manifest operations](../../src/regshape/libs/manifests/operations.py) and [exports](../../src/regshape/libs/manifests/__init__.py) | R1, R5, R6, R8: streamed consumption, decoding, cleanup |
| Stream-safe cache and retry ownership | [Transport middleware](../../src/regshape/libs/transport/middleware.py) | R6, R8: bypass live-stream caching, close discarded retry responses |
| Pre-handoff diagnostic ownership | [Request diagnostics](../../src/regshape/libs/decorators/call_details.py) | R8: close an acquired stream if metrics/debug output fails before returning it |
| Display normalization / deferred finalization | [Shared CLI progress](../../src/regshape/cli/progress.py) | R2-R5, R7, R11: row identity, output handoff and lifecycle |
| Command orchestration | [Manifest CLI](../../src/regshape/cli/manifest.py) | R3-R5, R7, R8: parse/extract/write inside display lifetime |
| Regression evidence | [Tests](../../src/regshape/tests/) | R9: library, frame and command-to-library coverage |
| References and guides | [Manifest API](../operations/manifests.md), [command](manifest.md), [callbacks](../../docs/library/transfer-progress.md), [guide](../../docs/guides/live-transfer-progress.md) | R10 |

Reuse shared length/cleanup logic where semantics match. If extraction is needed,
keep helpers private and domain-neutral; do not import a blob operation's private
implementation into manifests or change blob error types. Audit intermediate
streamed auth/redirect/retry response ownership in the transport; the cache and
retry fixes in D3.1 belong to the library delivery item. Keep all transport
changes narrowly scoped and regression-tested, not a new auth or retry policy.

#### D2. Public Python contract

Proposed additive signature:

```python
def get_manifest(
    client: RegistryClient,
    repo: str,
    reference: str,
    accept: str,
    *,
    progress_callback: ManifestProgressCallback | None = None,
) -> tuple[str, str, str]:
    ...
```

Keep all four positional parameters, tuple order, text semantics and missing-header
empty strings. With `None`, retain the existing buffered request/decoding path and
emit no observations. The command always supplies its observer, even when the
display is suppressed, so opt-out/nonterminal choices do not change download
consumption, error policy or request-debug behavior.

Add the following frozen event and string enums to the shared progress module;
export them and the callback alias through the manifest package as well:

| Field | Type | Contract |
| --- | --- | --- |
| `transfer_id` | `str` | Required nonempty opaque identifier, generated once per logical invocation; stable through all phases, unique across invocations |
| `kind` | `ManifestKind` | `UNKNOWN`, `MANIFEST`, or `INDEX`; enum values `unknown`, `manifest`, `index` |
| `phase` | `ManifestPhase` | `PREPARING`, `TRANSFERRING`, `RECEIVED`, `FAILED`, `CANCELLED`; lowercase enum values |
| `bytes_completed` | `int` | Absolute nonnegative consumed payload-byte count; booleans are not counters |
| `total_bytes` | `int \| None` | Comparable nonnegative size hint, otherwise `None`; booleans invalid; present total must not be less than completed bytes |

The public names are `ManifestProgress`, `ManifestKind`, `ManifestPhase` and
`ManifestProgressCallback = Callable[[ManifestProgress], None]`.
Invalid event construction raises `ValueError`, following the existing progress
model's validation style. Kind and phase must be enum members. There is no digest,
reference, direction, timestamp or attempt field: this is one download with no
new operation-level retry. Authentication retries remain inside the client and
do not reset counters or allocate rows.

Kind is advertised media-type classification, not payload validation. Known OCI
and Docker index/list content types map to INDEX; their supported manifest types
map to MANIFEST. Ignore media-type parameters for classification. Missing or
unsupported types remain UNKNOWN. UNKNOWN displays as Manifest; a later INDEX
classification updates the same row without changing its identity.

The blob `TransferProgress`, its required digest, enums, callback type and
verification semantics are unchanged. These in-process events are additive in
0.2, not a serialized protocol, persisted schema or telemetry message version.

#### D3. Observation and resource lifecycle

1. Emit PREPARING with zero bytes, unknown kind/total before the request.
2. Make the same manifest GET/Accept request through the client, with `stream=True`
   only when a callback is supplied. No HEAD, probe GET or body pre-read.
3. Apply existing HTTP error mapping. Error-body reading may use the existing
   diagnostic helper, but is not reported as successful manifest payload progress.
4. On success, classify headers and emit TRANSFERRING at zero bytes. Iterate
   `iter_content(chunk_size=64 * 1024)` with byte output; ignore empty keepalive
   chunks. Update absolute bytes and call the observer after each nonempty chunk.
5. Join collected chunks once, decode as D4, and close the owned response.
6. Emit RECEIVED only after all three steps succeed, then return the unchanged
   tuple. RECEIVED does not mean model-valid, digest-verified or command-complete.

Failure emits FAILED; user interruption such as KeyboardInterrupt emits CANCELLED.
Preserve the original exception, including its type/identity, when cleanup or
terminal notification also fails. An observer exception is propagated unchanged;
disable further observer calls rather than recursively reporting its own failure.
If cleanup alone fails, it is an error, not RECEIVED. No failure callback before
an observer exists and no fabricated event for command preflight validation.

Close owned responses on success, HTTP error, stream/decode error, cancellation
and callback failure. The operation owns the response returned to it; intermediate
auth/redirect/retry responses remain the client's responsibility. Buffered
no-observer responses also receive explicit cleanup without changing their
result data.

Ownership starts where a response is acquired, not only when the manifest
operation receives it. In `debug_call`, an acquired streamed HTTP response is
still decorator-owned until post-response metric/debug emission succeeds and
the response is returned. If that emission raises (including stderr or log-file
failure), close that exact response without consuming it, then propagate the
original diagnostic exception. If close also raises, preserve the original
exception's identity; do not rely on `get_manifest` or a stale `last_response`
to clean up a response they never received. Cover this boundary in both transport
modes, with successful close and secondary-close-failure injection. Audit
analogous pre-handoff response hooks without expanding telemetry coverage or
changing output schemas.

#### D3.1. Stream-safe middleware

The current optional cache stores a live `RegistryResponse`, including an unread
stream. Consuming and closing that response makes a later cached fetch unsafe.
For `request.stream=True`, bypass **both cache reads and writes**; do not
prebuffer the stream merely to cache it. Never insert an unbuffered response
(`body is None`) into this replayable-response cache. Preserve buffered GET cache
keys, expiration and reuse. Two observed fetches are two logical network fetches
with independent identities/responses, even if a buffered cache entry already
exists. This is an intentional compatibility difference when caching is enabled,
not an extra request solely for animation.

For configured status-code retries, the retry middleware owns each streamed
response it discards. Close it **before backoff or the next attempt**, without
draining its error body. The final successful or exhausted response remains owned
by the manifest operation. A close failure must not be hidden by retry handling;
preserve an already-active primary exception, otherwise surface the cleanup
failure instead of continuing silently. Preserve configured retry counts, status
codes, backoff and exception policy; do not add partial-body resumption.
Interruption during backoff must not leave the discarded response open.

Read-only synthetic probes of the baseline reproduced both hazards: a repeated
cached streamed GET raises `StreamConsumedError`, and a discarded streamed 503
remains open before a successful retry. These are design evidence, not implemented
fixes. Add repeated/mixed-mode fetch and retry-cleanup regressions in the library
item, including other streaming callers affected by the shared middleware guard.

#### D4. Byte and text semantics

Count bytes yielded by Requests, before text decoding. Compressed wire length
cannot be used as the decoded-payload total. A total is initially usable only for
an ASCII decimal Content-Length, absent Transfer-Encoding and identity/absent
Content-Encoding, matching the existing blob rule. Missing, malformed, negative,
encoded or otherwise noncomparable metadata means unknown, not zero.

Size is advisory, not a new integrity check. If consumption exceeds a usable
total, discard the total **before** emitting that counter. If successful EOF
falls short, discard it before RECEIVED. Log one standard warning for that
inconsistency; show actual bytes with unknown total. Do not clamp, synthesize
100%, silently return partial data after a transport error, or add an independent
size-rejection policy that changes raw-mode semantics. Requests framing errors
still propagate unchanged. A genuine empty body and total zero are valid
observations; later normal model parsing can still reject the empty body.

Preserve `response.text` behavior rather than choosing a new decoder. Use a private
metadata-only `requests.Response` adapter whose public `content` property returns
the collected bytes and whose public `encoding` copies the real response's
encoding. Its inherited text/apparent-encoding path handles detection and invalid
charset fallback. Do not mutate the real response's private `_content`, copy
Requests' decoder implementation, use character counts, or parse JSON to decode.
The adapter owns no socket and does not issue requests.

Design probe on Requests 2.31.0: 25 synthetic combinations of empty, ASCII,
UTF-8, UTF-16 and malformed byte payloads with absent, valid and invalid encodings
matched the existing text property. This is compatibility evidence, not proof for
all encodings or dependency versions; promote it into regression tests and test
with the supported dependency environment during implementation.

#### D5. Display adaptation and command completion

Extend `ProgressDisplay.observe` to accept either event type. Normalize them into
a small private rendering view; preserve blob keys `(blob, digest)` and use
manifest keys `(manifest, transfer_id)`. Never key a manifest row by discovered
digest or advertised kind. Blob counters, deduplication and terminal behavior
remain unchanged; manifest rows must not increment blob done/skipped counters.

Use Manifest/Index plus a per-manifest ordinal as the row prefix. Known totals
enable percentage/bar and, when meaningful, estimated remaining time. Unknown
totals show bytes and optionally rate. Rate measures consumed payload bytes over
elapsed observation time, not guaranteed wire throughput. Zero total does not
divide by zero. No artificial redraw delay or duplicate lifecycle lines.

For manifest observations, RECEIVED is a **pending** row, not finalized success.
The command explicitly advances that active row to Processing (unless raw),
Writing and finally Downloaded after the existing output helper returns,
including file close. Keep those command stages private to the CLI; do not add
them to the library event enum or teach the library about terminal output.
The private completion operation rejects a missing/wrong active manifest or
completion before receipt. A display context must not auto-promote a pending
manifest row to Downloaded on ordinary exit. In the command, every successful
path explicitly completes it, tested by raw, full and part output cases.

Result emission needs its own output handoff: stdout and stderr commonly share
one terminal even though separate captures hide that fact. Before any stdout
result write, clear the active row **through stderr** and suspend progress
redraws. Leave the existing output helper and stdout bytes untouched; do not
send the payload through the stderr diagnostic proxy. Keep the logical row/key
pending while forgetting its old physical cursor footprint. After successful
emission, restore/finalize that one row below the result at the new cursor
position, never by moving upward across payload lines.

On partial-output failure or interruption, preserve every payload character
already written. Rebase safely before displaying Failed/Cancelled; a separating
newline through stderr is allowed when the cursor boundary is unknown, but no
cursor-up/erase may target result lines. Preserve the output exception if this
cleanup also fails. Apply the handoff consistently to raw, full and part stdout
paths, without fragile file-descriptor identity assumptions; it can conservatively
suspend even when stdout is redirected or on a different terminal. Suppressed
rendering is a no-op, and file output needs no stdout handoff. This coordinates
this command's writes, not unrelated processes concurrently writing the terminal.

Move expected post-receipt parse/part error emission outside the display context:
carry the existing message/exit code through a private command-failure exception,
then use the existing error helper after cleanup. This prevents a missing part's
SystemExit(2) from being misclassified as user cancellation. Unexpected transport,
callback and output exceptions are not replaced with success-shaped defaults.
Keep existing error formatting, traceback behavior where applicable and exit
codes; do not introduce a blanket exception handler.

Nest existing scenario/diagnostic handling consistently with blob commands.
A secondary renderer/logging failure cannot replace an already-active primary
error. Successful payload emission is not transactional with later independent
shell or process failure; no promise extends beyond required command work and
display-context cleanup. Failed final label/newline writes must not retain a
successful row or duplicate it.

#### D6. Compatibility and reuse boundaries

No model parsing/digest verification is added to raw/library fetches. Keep
Content-Type and Docker-Content-Digest result values unchanged, even if absent.
The command still parses nonraw results and rejects unsupported/missing parts
with its existing behavior. Output is still UTF-8 text with the existing newline
rules; no atomic file replacement or stdout rollback is added.

Keep content negotiation, credentials, request path, redirects and timeouts under
the existing client. Intentional transport changes are streamed consumption for
observers, the optional-cache bypass and discarded-response cleanup in D3.1,
with the diagnostic implications in section 11. Disabled rendering still observes
the stream; omitted library callbacks do not. Existing blob/layout/Docker
progress/output contracts remain unchanged; the shared stream-safety guard also
applies to their optional caching/retry paths and must be regression-tested.

Future #91 can observe each manifest fetch, perform its own required validation
and storage, and explicitly complete that row. This does not yet implement graph
deduplication, descriptor verification, aggregate object counts or concurrency.

### PM-1. Traceability

| Requirement / scenario | Design | Delivery / verification |
| --- | --- | --- |
| R1, R2 / S4 | D2-D4 | Library item; event validation, no-observer and intermediate-read tests |
| R3, R4 / S1, S3 | D5 | Command item; terminal frames and post-receipt failure tests |
| R5 / S1-S4 | D2, D4-D6 | Library/command items; tuple, decoding, aliases, shared-terminal result safety and exit-code comparisons |
| R6 / S1, S2 | D3-D4 | Library/command items; length, rate and request-count assertions |
| R7 / S2, S3 | D5, section 11 | Command item; suppression, diagnostics, throttling and width tests |
| R8 / S3, S4 | D3, D3.1, D5 | Library cache/retry regressions and both implementation items' callback/cancellation/cleanup fault injection |
| R9 / all | PM-7 | Both implementation items plus required hosted regression gates |
| R10 / all | Sections 7, 11, 14 and PM-6 | Final documentation item and later inventory close-out |
| R11 / S4 | D2, D5-D6 | Stable identities and sequential reuse tests; #91 owns orchestration |

Every changed component in D1 maps to these requirements. No new telemetry service,
auth policy or artifact traversal is justified by this feature.

### PM-2. API and errors

D2 defines the complete additive Python surface; the HTTP endpoint remains
`GET /v2/<repo>/manifests/<reference>` with existing Accept negotiation. No server
API version, queue, database or cache-key version changes. HTTP errors preserve
the existing AuthError/ManifestError mapping; request failures remain Requests
exceptions. Callback exceptions remain the caller's original exceptions.
GET is nonmutating; this feature adds no retries or resumption. CLI runtime errors
retain exit 1, missing/unsupported selected parts and usage errors retain exit 2,
and Click interruption behavior remains unchanged.

### PM-3. Metrics, usage and resource cost

Acceptance metrics in section 8 map to event traces, synthetic request counters,
visible terminal frames and fixture comparisons. Existing optional operation and
scenario timing measures elapsed work; request timing/preview follows section 11.
No registry data or progress histories are persisted by this feature.
Local cost is O(payload size) memory plus synchronous callback/render work, with
bounded routine redraw frequency and no additional progress-only requests.
Hosted checks consume the already-authorized Actions capacity.

> NOTE: There is no backend customer identity, longitudinal usage/frequency/churn
> store or adoption instrumentation in scope. This is an explicit gap against a
> generic hosted-service metrics rubric, not a claim that local counters measure
> adoption. Adding such collection would require a separately approved product
> and privacy design. Runtime memory/CPU dollar costs have not been benchmarked;
> they are borne by the caller, with the bounded-work method described above.

### PM-4. Authentication, authorization and networking

Use the existing credential resolution and token challenge flow with repository
pull access. No identities, scopes or role assignments are added. Network access
remains caller-initiated to the chosen registry/token endpoints through existing
transport policy and caller firewall/proxy controls. No virtual network, private
endpoint, inbound port or regional infrastructure is provisioned. Secrets remain
outside events and row labels; progress does not refresh expired credentials.

### PM-5. Failure taxonomy

| Condition | Library behavior | Command/display behavior |
| --- | --- | --- |
| Invalid reference or raw+part | Existing preflight failure | Existing diagnostic/exit; no fake transfer row |
| HTTP 401/404/other error | Existing typed error; close response | Failed row if started; existing diagnostic after cleanup |
| Stream timeout/framing failure | Original request exception; close | Failed, never RECEIVED/Downloaded |
| Repeated observed fetch with caching enabled | Bypass cache reads/writes; fresh owned response per logical fetch | Independent transfer row; no exhausted cached stream |
| Retryable status discarded before another attempt | Close streamed response before backoff; preserve retry policy | Preparing until a usable response; cleanup failure/cancellation cannot leak discarded attempts |
| Missing/compressed length | Unknown total; actual bytes | Byte-only progress; no false percentage/remaining time |
| Inconsistent comparable length | Warning once; invalidate total, retain actual bytes | Unknown total; existing parsing/output policy decides success |
| Empty or malformed payload | Raw/library text behavior unchanged | Nonraw parser failure stays Failed; raw may succeed |
| Callback failure | Preserve original exception; stop notifying | Failed where possible; preserve primary error |
| User interrupt | Cancel, close, propagate | Same row Cancelled; existing interruption behavior |
| Missing/unsupported part | Not a fetch failure | Failed plus existing exit 2; never Cancelled |
| Output/close failure | Fetch already received | Failed; partial output may remain; original error behavior |
| Metrics/debug emission fails before response handoff | Diagnostic owner closes acquired stream; preserves original exception if close fails | Failed if started; operation is not expected to close an unseen response |
| Result stdout shares the progress terminal | No library change | Clear/suspend via stderr before output; rebase without erasing payload, then finalize the same logical row |
| Diagnostic/render cleanup failure | Do not mask active primary failure | Cleanup without duplicate success; propagate sole failure |

One logical download is sequential. No new locks, contention policy, rate-limit
handling, pagination, cold-start service or concurrent rendering is introduced.

### E. Risk checklist

| Area assessed | Risk / mitigation |
| --- | --- |
| API/output compatibility | Charset and completion drift: D2/D4/D5 and paired baseline fixtures |
| Security/auth | Untrusted metadata, injected labels or exposed credentials: fixed labels, advisory totals, unchanged auth |
| Privacy | New data collection avoided; events omit payloads/headers; existing caller-owned logs only |
| Dependencies | Requests decoding behavior: public-property adapter and dependency-environment tests |
| Infrastructure/deployment | No infrastructure change; existing package pipeline only |
| Resource lifecycle | Stream/error/observer/retry and pre-handoff diagnostic cleanup: D3/D3.1 ownership plus fault-injection/repeated-fetch tests |
| Shared-terminal output | Unterminated row collides with stdout: D5 clear/suspend/rebase handoff, including preserved partial output |
| Multi-tenancy/noisy neighbor | No hosted tenants; slow callbacks and large result memory remain caller-local |
| Regression | Shared display/telemetry changes: all existing blob/layout/Docker regression suites |

Implementation work-item owners own the first, second, fourth, sixth and eighth
mitigations; the documentation item owns accurate communication of all limits.

### F. Breaking changes and notification

No result-schema or existing-option removal. Adding interactive manifest stderr
output and suppressing request-layer body previews for streamed CLI fetches are
intentional observable changes. `--no-progress` disables animation, not streaming
or request-preview suppression. Explain these differences in final documentation.
Library callers omit the callback to retain buffered request behavior. Existing
buffered cache behavior is also retained. With caching enabled, observed fetches
intentionally bypass it and can perform more network requests than buffered
cache hits; disabling animation does not disable that safety rule. Existing
artifact alias deprecation text is unchanged. Shared regressions gate release.

### G. Security risk management

Treat headers/body as untrusted; do not interpolate either into terminal control
sequences. Use no new cryptography, keys, credential storage or secret logging.
No new size cap is invented for a complete-text API; document memory risk and
ensure failure cleanup. Preserve explicit insecure/break-mode boundaries and
existing transport policy. Tests use synthetic public fixtures, not credentials.

### H. Privacy and data sovereignty

Progress is process-local and stderr-only when displayed; no telemetry backend
or retention policy is added. Files/logs remain where the caller already chose,
and network traffic goes only through existing registry/auth flows. No new
cross-region data transfer, customer identifier or progress-history storage.

### I. Operational diagnosis

Use phases/bytes to distinguish receiving, local processing and writing. Check
normal errors first for authentication, parser/part or filesystem failures.
Optional existing telemetry assists diagnosis without pre-reading the stream.
Automated frame/counter/cleanup tests enforce health properties; there is no
hosted on-call monitor or availability promise for this local feature.

### J. Dependencies

No new package dependency or minimum-version change. Retain Python >=3.10,
Click >=8.1.0 and Requests >=2.31.0. Validate the additive decoder/event contracts
against installed supported dependencies and existing CI. A Requests decoding
regression is a delivery blocker, not permission to silently change raw encoding.

### K. Deployment and buildout

Ship through the existing library/CLI packaging and release automation after
the authorized implementation and documentation gates. No manual service setup,
data migration or configuration backfill. This design PR neither changes package
version nor publishes 0.2. Agent-only reviews do not waive required hosted checks
or the owner's design/merge authorization.

### L. Multi-tenancy

Not a multi-tenant hosted service. Events/displays are scoped to a process and
operation; no shared tenant state is introduced. Parallel downloads and fairness
policies are outside this sequential feature.

### M. Regression coverage

Preserve blob digest validation, per-blob deduplication/counts and finalization;
layout/Docker preparation and child propagation; stderr logging/telemetry
coordination; failed final flush behavior; artifact option aliases and conflicts;
manifest raw/part/file/Accept/output semantics; buffered cache reuse and safe
stream bypass; discarded retry-response closure before waits; diagnostic-boundary
closure; shared stdout/stderr cursor safety. Tests must exercise real command
orchestration with a mocked streamed transport, not only a mocked final tuple.
Review changed source documentation alongside behavior without rewriting
unrelated legacy declarations.

### N. Rollback

Before delivery, this document/inventory proposal can be withdrawn without runtime
effect. After delivery, callers can disable animation with `--no-progress`, but
that does not roll back streaming; library callers can omit the callback.
A release rollback/revert of command wiring restores buffered CLI consumption,
subject to normal authorization and validation; retain the shared stream-safety
fixes rather than reintroducing known cache/retry hazards. Once public event exports ship,
do not remove them in an unrelated patch. There is no stored-data migration to
undo; already emitted stdout or partial files cannot be transactionally undone.

### PM-6. Work breakdown and estimates

| Order | Work item | Dependencies | Proposed branch | Relative effort |
| --- | --- | --- | --- | --- |
| 1 | [#101: design and Planned inventory](https://github.com/toddysm/regshape/issues/101) | Existing merged foundations | `prd/manifest-get-progress-fd2ca782` | Small, document review and checks |
| 2 | [#105: library observations](https://github.com/toddysm/regshape/issues/105) | #101 merged and Build authorized | `feature/manifest-get-progress/library-observations-fd2ca782` | Medium, including decoding/lifecycle, cache/retry and diagnostic-boundary tests |
| 3 | [#104: renderer and command](https://github.com/toddysm/regshape/issues/104) | #105 | `feature/manifest-get-progress/command-renderer-fd2ca782` | Medium, including stdout handoff; highest shared-regression risk |
| 4 | [#102: final user documentation](https://github.com/toddysm/regshape/issues/102) | #105 and #104 | `feature/manifest-get-progress/user-docs-fd2ca782` | Small, including examples/link validation |

Each delivery estimate includes implementation, focused tests/type/lint checks,
source-documentation updates, review fixes and hosted validation. Exact
person-days/dates are not committed; stream cleanup and display error paths are
the long poles. Work items are native children of #90 with native dependencies;
scheduling #90 before #91/#88 does not itself create dependencies.

After delivery, a separately authorized `chore/manifest-get-progress-close-out`
pull request must move the inventory to Implemented before the feature closes.
That is not a fifth delivery item or authorization to merge anything now.

### PM-7. Test plan and CI

Library tests cover:

- Ordered PREPARING/TRANSFERRING/RECEIVED events, stable opaque identity, distinct
  calls, advertised kinds and unchanged tuple/header values.
- A 256 KiB stream consumed in 64 KiB chunks: intermediate counts before EOF and
  no `.text`/`.content` prebuffer on the live response.
- Known, unknown, malformed, encoded, zero and inconsistent length metadata;
  no impossible percentage or hidden stream failure.
- The 25 decoding combinations above, no-observer compatibility, supported
  manifest/index and raw non-image payloads.
- HTTP errors, auth retries, both transport modes, interruption and callback/
  response-close failures, with primary exception identity and closure assertions.
- Repeated observed fetches with caching enabled, including a warm buffered
  cache: each stream is fresh, consumable and closed, with no cached unbuffered
  response. Repeated no-observer fetches retain buffered cache reuse.
- Retryable 5xx then success, repeated failures through exhaustion, backoff
  interruption and discarded-response close failure: close discarded streams
  before waiting/retrying and leave the final response for operation cleanup.
  Exercise shared blob-stream paths without changing retry/backoff policy.
- In both transport modes, inject metrics/debug stderr/log-file failure after
  acquiring a stream but before returning it; assert closure and original
  diagnostic exception identity, including when close itself fails.

Command/display tests cover:

- Tag/digest references; existing aliases; default/custom Accept; full, raw,
  part and file output; stdout parseability and byte-for-byte result comparisons.
- The same row across kind discovery, download, processing, writing, failure,
  cancellation and completion. Validate visible frames using the existing
  terminal emulator, including widths/resizing and final flush failure.
- No Downloaded frame before output completion; missing parts remain exit 2
  and Failed. Small/empty bodies add no artificial delay.
- Opt-out, nonterminal stderr and structured telemetry; ordinary JSON stdout
  does not suppress progress. Suppression leaves streamed semantics unchanged.
- Text telemetry, configured/fallback stderr logging, nested scenario errors,
  output failures and original-exception preservation.
- Send stdout and stderr through the same terminal emulator, with separately
  recorded channel bytes. Verify clear/suspend/rebase across multiline/raw/part
  output, wrapping, resizing, partial writes and interruption: no stale Writing
  row, no result character erased, exactly one final logical row and no terminal
  controls in stdout. Retain separate-stream, redirected and file-output tests.
- Existing blob, upload, layout/Docker, artifact-option and transport regressions.

Use existing pytest suites and scoped type/lint tooling; no new test framework.
The existing hosted Python 3.10/3.11/3.12 matrix, minimum-Click job and CodeQL
checks remain binding. Extend the minimum-Click selectors for new command tests
where needed during delivery. Current baseline: 1,943 tests per Python matrix
job and 360 minimum-Click tests; these counts describe the baseline, not this
unimplemented feature's results.

For this documentation-only proposal, check links/anchors, current signatures/help,
shell-example syntax, output-format facts, decoder evidence and whitespace.
There is no configured documentation builder or Markdown linter; do not claim
one ran. Hosted checks on the proposal validate the unchanged runtime, not the
future implementation. Final user docs must be rechecked against delivered code.

### O. Next actions and launch gate

Owner approval of revision 3 and the native breakdown is recorded in the feature
ledger, and the design PR is merged. After the reviewed inventory-start transition,
implement library item #105 and leave its implementation PR open for owner review.
Other delivery work and implementation merges require later authorization. Delivery, user documentation,
inventory close-out and eventual release are distinct gates; no partner/legal
integration or regional launch task is introduced.

### P. References

- [Feature requirements and ledger](https://github.com/toddysm/regshape/issues/90).
- [Existing progress design](https://github.com/toddysm/regshape/issues/72).
- [Artifact options design](artifact-options.md).
- [Current manifest API reference](../operations/manifests.md).
- [Current command reference](manifest.md).
- [Current callback reference](../../docs/library/transfer-progress.md).
- [Current progress guide](../../docs/guides/live-transfer-progress.md).
- [Capability inventory](../../docs/project-state.md).

### Appendix: rejected alternatives

- Reusing a blob event with a tag or fabricated digest breaks its validation and
  verification contract; a separate additive event is explicit and compatible.
- Adding a HEAD solely for size/digest costs another request and still cannot
  guarantee a stable tag; opaque identity needs neither.
- Showing a bar after `response.text` is buffered is not incremental progress.
- Finalizing at RECEIVED incorrectly reports success for parser/part/output
  failures; command-owned completion is required.
- Hardcoding UTF-8 or using character-count streaming changes existing raw text
  behavior; reuse Requests' decoder over observed bytes instead.
- A new progress package or custom terminal stack duplicates proven coordination;
  extend the current renderer without relaxing blob semantics.
