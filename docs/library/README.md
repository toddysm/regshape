# Python SDK overview

<!-- doc-type: overview -->

Use `regshape.libs` directly to build Python tools without a Click command
context. The libraries own every capability; the CLI only adapts user input and
presentation. These entry points were delivered by
[#163](https://github.com/toddysm/regshape/issues/163) for development builds
targeting 0.2, not a release announcement. Click is still installed for the CLI,
but importing the library does not import Click or `regshape.cli`.

## Entry points

- **Telemetry:** `TelemetryConfig` and `telemetry_session` in
  `regshape.libs.decorators` configure scoped diagnostics and cleanup; see
  [managed telemetry scopes](telemetry.md#managed-telemetry-scopes).
- **Authentication:** `verify_credentials` and `login` in `regshape.libs.auth`
  verify explicit credentials, with optional persistence through login; see
  [credential verification](auth.md#verify_credentials) and [login](auth.md#login).
- **Manifests:** `regshape.libs.manifests` owns
  [protocol defaults](manifests.md#protocol-defaults) and `resolve_digest` for
  [tag-to-digest resolution before listing referrers](manifests.md#resolve-a-tag-and-list-referrers).
- **References:** `regshape.libs.refs` provides
  [digest classification and validation](../guides/artifact-options.md#classify-and-validate-sdk-references)
  and [plain-repository parsing with an unsafe-input guard](../guides/artifact-options.md#parse-plain-repositories-and-reject-unsafe-input).
- **Layout preparation and updates:** `prepare_layer_content` in
  `regshape.libs.layout` prepares bytes and media types for
  [staging layers](../guides/create-oci-layout.md#prepare-and-stage-a-layer-from-python);
  `update_config_with_result` reports
  [whether this update made the manifest stale](../guides/create-oci-layout.md#post-generation-update-helpers),
  not its overall validity or pre-existing staleness.
- **Blob results:** `upload_blob_stream_result`, `upload_blob_chunked_result`
  and `mount_blob_result` in `regshape.libs.blobs` return library-owned
  [upload/mount result metadata](transfer-progress.md#upload-and-mount-results).
- **Ping:** `ping` and `PingResult` in `regshape.libs.ping` expose connectivity
  and opt-in [authentication-required results](ping.md#ping).
- **Docker push:** `push_image` in `regshape.libs.docker` accepts a
  [caller-provided registry client](../guides/docker-desktop-integration.md#supplying-a-registry-client-from-python)
  and returns library-owned push totals.

Each linked reference documents inputs, results, errors and compatibility.
Additive result APIs preserve the older descriptor/digest-string APIs; consult
the operation reference before opting in to new result shapes or behavior.

## Related documentation

- [Library/CLI architecture boundary](../../specs/architecture.md#librarycli-boundary)
- [Capability inventory and delivery evidence](../project-state.md#capability-inventory)
- [All guides and references](../index.md#guides-and-references)
