# Use artifact-oriented command options

<!-- doc-type: how-to -->

RegShape's command-line interface (CLI) prefers **`--artifact` / `-a`** when
selecting Open Container Initiative (OCI) registry artifacts, manifests, or
repositories in the commands below. The reference itself does not
change: it still includes the registry and repository, with the appropriate
tag or digest.

This interface is implemented on the development branch and targets release
**0.2**. Implementation is not release publication. Check your installation:

```bash
regshape manifest get --help
```

Help must list `--artifact`, `-a`, `--image-ref`, and `-i`. An older installed
package may not have the new spellings; use its existing flags or install a
version containing this change. Python library consumers require Python 3.10
or newer, as before.

## 1. Identify the affected commands

| Commands | What `--artifact` selects |
| --- | --- |
| `manifest get`, `info`, `descriptor` | A manifest/index by the existing registry/repository tag or digest reference |
| `manifest put` | The existing registry/repository target; input still comes from `--file` or `--stdin` |
| `manifest delete` | A digest reference; tag references remain rejected |
| `tag list` | A repository; existing tag/digest suffixes are still ignored |
| `tag delete` | A tag target; digest references remain rejected |
| `referrer list` | A manifest by tag or digest; tag resolution still uses a manifest HEAD request |

Use explicit tags for tag deletion to avoid an unintended default-tag target.
Omitted-tag/default-`latest` behavior and other existing parser rules have not
been redesigned by this feature.

An artifact selector does not imply a complete artifact download. Supported
media types, raw/model parsing, and command-specific restrictions are unchanged.
Full artifact download, canonical blob-reference consolidation and manifest
GET progress remain separate work: [#91](https://github.com/toddysm/regshape/issues/91),
[#88](https://github.com/toddysm/regshape/issues/88), and
[#90](https://github.com/toddysm/regshape/issues/90).

## 2. Replace the flag, not its value

For registry access, use a repository you are authorized to read and the same
credential configuration as before. Replace the example hostname/repository
with your own. The following read-only examples do not change registry content.

Before:

```bash
regshape manifest get --image-ref registry.example.com/team/package:v1
```

Preferred:

```bash
regshape manifest get --artifact registry.example.com/team/package:v1
```

The short form is `-a`:

```bash
regshape referrer list -a registry.example.com/team/package:v1 --json
regshape tag list -a registry.example.com/team/package --all
```

All four spellings address the same option and feed the same underlying
operation. The result data, request count, authentication, and applicable
progress behavior remain unchanged.

For a repository containing `v1` and `v2`, this command:

```bash
regshape tag list --artifact registry.example.com/team/package
```

prints the same stdout result as its legacy spelling:

```text
v1
v2
```

Verify a migrated script by comparing its result and exit status using the same
reference and other flags. Do not compare timing values from separate telemetry
runs as though they must be identical.

## 3. Understand deprecation and conflicting values

`--image-ref` and `-i` are **deprecated starting in 0.2 but remain functional**.
Their help/documentation notice is:

```text
Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a. Retained for compatibility.
```

There is **no runtime deprecation warning** and **no scheduled removal version**.
Only the old registry-reference aliases are deprecated, not the new `--artifact`
option. Existing scripts can be migrated incrementally.

Matching repetitions are accepted and execute one operation:

```bash
regshape manifest get --artifact registry.example.com/team/package:v1 \
  --image-ref registry.example.com/team/package:v1
```

Different values are rejected, including repetitions of only an old spelling.
This intentionally replaces the previous last-value-wins behavior for ambiguous
commands. The comparison uses the exact supplied strings: a bare repository
and the same repository with `:latest` are different inputs for this check.

For example:

```bash
regshape manifest get --artifact registry.example.com/team/package:v1 \
  -i registry.example.com/team/package:v2
```

produces stderr output and exits with code **2**, before credential lookup or
registry requests:

```text
Usage: regshape manifest get [OPTIONS]
Try 'regshape manifest get --help' for help.

Error: Conflicting reference options. Supply one artifact reference.
```

Recovery: remove the extra target or make the supplied values identical. Do not
depend on ordering to select one of several different targets.

Missing values and unknown options retain Click's normal usage errors. Later
registry/transport/parsing errors keep their existing command behavior.

## 4. Leave unrelated options alone

Do not globally replace every `-i` in scripts:

- `docker export --image/-i` and `docker push --image/-i` still select a **local
  Docker image** and are not deprecated.
- Layout layer `--index/-i` still selects a layer index.
- `layout push --dest` and `docker push --dest` remain destination options.
- Blob commands still use separate `--repo/-r` and `--digest/-d` options.
- `referrer list --artifact-type/-t` still filters artifact type; it is not the
  target reference.
- OCI Image Manifest, Image Index, platform and media-type names are not renamed.

No new `--reference` or `--artifact-ref` synonym or positional target is added.
Root options such as `--insecure` still go before the command group.

## Python parser naming

Use the additive `parse_artifact_ref` entry point for artifact-oriented tooling:

```python
from regshape.libs.refs import format_ref, parse_artifact_ref, parse_image_ref

reference = "registry.example.com/team/package:v1"
parts = parse_artifact_ref(artifact_ref=reference)
assert parts == ("registry.example.com", "team/package", "v1")
assert format_ref(*parts) == reference

# Existing positional and keyword callers remain compatible.
assert parse_image_ref(reference) == parts
assert parse_image_ref(image_ref=reference) == parts
```

The artifact-named function delegates to the image-named parser. Both keep the
`(registry, repository, tag_or_digest)` result and default `latest` tag.
Both now reject unsafe input before parsing; neither validates digest payloads
or the complete OCI reference grammar.
The old Python function is retained; the CLI flag deprecation does not declare
that Python interface deprecated. Downgrading to a package without the new entry point
requires using `parse_image_ref` again.

### Classify and validate SDK references

The 0.2 development interface adds `is_digest` and `validate_digest` to
`regshape.libs.refs`. Apply them to the parsed final token, not a full reference:

```python
from regshape.libs.errors import ReferenceValidationError
from regshape.libs.refs import is_digest, parse_artifact_ref, validate_digest

registry, repo, token = parse_artifact_ref("registry.example.com/team/package:v1")
assert not is_digest(token)
assert is_digest("sha384:abc")  # Digest-shaped, but not a supported digest.

try:
    validate_digest("sha384:abc")
except ReferenceValidationError as error:
    assert str(error) == "digest references must use sha256 or sha512"

digest = "sha256:" + "a" * 64
assert validate_digest(digest) == digest
```

Classification does not prove validity: digest-shaped tokens contain a colon,
which OCI tag names cannot contain. Validation accepts only lowercase SHA-256
with 64 hexadecimal characters or SHA-512 with 128, without rewriting the value.
The parsers keep their return shape and do not validate digest payloads.
`format_ref` uses this classification without validating: unsupported/malformed
digest-shaped tokens use `@`, rather than being formatted as tags.

The SDK's `delete_manifest` validates digests by default; `delete_tag` rejects
digest-shaped inputs, including unsupported algorithms. Their
`ReferenceValidationError` is a `ValueError` subclass; the CLI renders it as
exit code 2. For preflight before constructing a transport client, use
`regshape.libs.manifests.validate_manifest_digest(token)` or
`regshape.libs.tags.validate_tag_reference(token)`; each returns the unchanged
token or raises the same operation-specific validation error. The CLI uses these
checks before credential lookup, and the operations independently enforce them.
Both operations accept keyword-only `strict=False` for deliberately
invalid registry-conformance requests. That opt-out does not make an invalid
request safe and is not exposed as a new CLI flag. See the
[manifest API](../../specs/operations/manifests.md#delete_manifest) and
[tag API](../../specs/operations/tags.md#delete_tag) for the mutating operations.

### Parse plain repositories and reject unsafe input

Use the SDK's `parse_repository_ref` when a separate digest identifies a blob:

```python
from regshape.libs.refs import parse_repository_ref, reject_unsafe_reference

assert parse_repository_ref("localhost:5000/team/package") == (
    "localhost:5000", "team/package",
)
reject_unsafe_reference("registry.example.com/team/package:v1")
```

Ports and nested repository paths are preserved. Tags (including `:latest`),
digest suffixes and trailing slashes are rejected with `ValueError`; trailing
slashes remain rejected as in the existing blob CLI, rather than being stripped.
The five blob commands use this helper and report the same suffix error, with
exit code **1**:

```text
Error [registry.example.com/team/package:latest]: Repository must be a plain 'registry/repository' without a tag or digest
```

`reject_unsafe_reference` is also available on its own. It returns `None` for
safe syntax and raises `regshape.libs.errors.UnsafeReferenceError`, a `ValueError`
subclass, for empty input, URLs (including single-slash and protocol-relative
forms), leading slashes, userinfo-like input, whitespace, Unicode control and
format characters (including C0/C1, bidi overrides, zero-width spaces and byte
order marks), backslashes, `?`, `#`, or `%`. Digest syntax requires a single `@`
after the repository followed by a colon-bearing token without slashes; payload
validation remains separate. Errors omit the input, and CLI adapters also omit
unsafe values from their error context. Missing registry/repository errors keep
their existing behavior for otherwise safe input.

All three parsers apply this guard by default. SDK conformance tools can pass
keyword-only `strict=False` to bypass **only** the safety guard; the plain-repo
check still applies. This opt-out restores permissive parsing, not a safe
request, and adds no CLI flag. Do not send untrusted values or log credentials
when deliberately bypassing it. This additive helper does not implement the
canonical-reference redesign in [#88](https://github.com/toddysm/regshape/issues/88).

## Output, validation and troubleshooting

JavaScript Object Notation (JSON), raw text, field extraction, file output,
existing telemetry, and progress
remain separate as before. The new aliases add no diagnostic output during a
successful command. A conflict writes only a generic usage diagnostic, without
echoing either supplied target.

If help reports an unknown `--artifact`, check which executable/environment you
are running. Do not change credentials to repair an unsupported option.
Conversely, changing aliases does not refresh an expired registry token or fix
a credential-helper failure.

Migration changes only script option names; no registry-content or credential
migration/cleanup is required. Existing put/delete commands remain mutating
operations and require the same care and permissions as before.

Automated compatibility checks exercise all eight commands, request/output
parity, error paths and the declared Click 8.1.0 floor with synthetic network
responses. These checks do not certify every live registry.

## References

- [Manifest commands](../../specs/cli/manifest.md)
- [Tag commands](../../specs/cli/tag.md)
- [Referrer commands](../../specs/cli/referrer.md)
- [Functional and engineering design](../../specs/cli/artifact-options.md)
- [Live transfer progress](live-transfer-progress.md)
- [Docker integration](docker-desktop-integration.md)
