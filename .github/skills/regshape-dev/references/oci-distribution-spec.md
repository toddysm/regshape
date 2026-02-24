# OCI Distribution Spec Reference

Quick reference for the OCI Distribution Specification v1.1.0 APIs relevant to RegShape.

## Table of Contents

- [Base URL](#base-url)
- [Authentication Flow](#authentication-flow)
- [API Endpoints](#api-endpoints)
- [Manifest Operations](#manifest-operations)
- [Blob Operations](#blob-operations)
- [Tag Operations](#tag-operations)
- [Catalog / Referrers](#catalog--referrers)
- [Common Headers](#common-headers)
- [Error Response Format](#error-response-format)
- [OCI Media Types](#oci-media-types)
- [OCI Image Manifest Schema](#oci-image-manifest-schema)
- [OCI Image Index Schema](#oci-image-index-schema)

## Base URL

All endpoints are relative to the registry base URL:

```
https://<registry-host>/v2/
```

The `/v2/` endpoint is used to check API version support. A `200 OK` response confirms OCI Distribution Spec support.

## Authentication Flow

1. Client makes a request to a registry endpoint
2. If unauthorized, registry responds with `401 Unauthorized` and a `WWW-Authenticate` header
3. The `WWW-Authenticate` header specifies the auth scheme:
   - `Basic realm="..."` — use HTTP Basic Auth
   - `Bearer realm="...",service="...",scope="..."` — exchange credentials for a token
4. For Bearer: POST/GET to the `realm` URL with `service`, `scope`, and credentials
5. Token response: `{"token": "...", "expires_in": 300, "issued_at": "..."}`
6. Use the token in subsequent requests: `Authorization: Bearer <token>`

### Scope Format

```
repository:<name>:<actions>
```

Actions: `pull`, `push`, `delete`, `*` (comma-separated for multiple)

Example: `repository:myrepo/myimage:pull,push`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v2/` | API version check |
| HEAD/GET | `/v2/<name>/manifests/<reference>` | Get manifest |
| PUT | `/v2/<name>/manifests/<reference>` | Push manifest |
| DELETE | `/v2/<name>/manifests/<reference>` | Delete manifest |
| HEAD | `/v2/<name>/blobs/<digest>` | Check blob existence |
| GET | `/v2/<name>/blobs/<digest>` | Pull blob |
| DELETE | `/v2/<name>/blobs/<digest>` | Delete blob |
| POST | `/v2/<name>/blobs/uploads/` | Initiate blob upload |
| PATCH | `/v2/<name>/blobs/uploads/<session-id>` | Upload blob chunk |
| PUT | `/v2/<name>/blobs/uploads/<session-id>?digest=<digest>` | Complete blob upload |
| GET | `/v2/<name>/tags/list` | List tags |
| GET | `/v2/<name>/referrers/<digest>` | List referrers |
| GET | `/v2/_catalog` | List repositories (non-standard, widely supported) |

## Manifest Operations

### Get Manifest

```
GET /v2/<name>/manifests/<reference>
Accept: application/vnd.oci.image.manifest.v1+json
```

`<reference>` can be a tag (e.g., `latest`) or a digest (e.g., `sha256:abc123...`).

### Push Manifest

```
PUT /v2/<name>/manifests/<reference>
Content-Type: application/vnd.oci.image.manifest.v1+json

{manifest JSON body}
```

Response includes `Location` header and `Docker-Content-Digest`.

### Delete Manifest

```
DELETE /v2/<name>/manifests/<digest>
```

Must use digest, not tag.

## Blob Operations

### Check Blob Existence

```
HEAD /v2/<name>/blobs/<digest>
```

Returns `200 OK` with `Content-Length` if exists, `404` otherwise.

### Pull Blob

```
GET /v2/<name>/blobs/<digest>
```

Returns blob content. May return `307` redirect to external storage.

### Monolithic Upload

```
POST /v2/<name>/blobs/uploads/
```

Response: `202 Accepted` with `Location` header containing upload URL.

Then complete with:

```
PUT <location>?digest=<digest>
Content-Type: application/octet-stream

{blob data}
```

### Chunked Upload

```
POST /v2/<name>/blobs/uploads/
```

Then upload chunks:

```
PATCH <location>
Content-Range: <start>-<end>
Content-Type: application/octet-stream

{chunk data}
```

Complete with:

```
PUT <location>?digest=<digest>
```

### Cross-Repository Blob Mount

```
POST /v2/<name>/blobs/uploads/?mount=<digest>&from=<source-repo>
```

Returns `201 Created` if mount succeeds, `202 Accepted` if fallback to upload.

## Tag Operations

### List Tags

```
GET /v2/<name>/tags/list
```

Response:

```json
{
  "name": "<name>",
  "tags": ["latest", "v1.0", "v2.0"]
}
```

Supports pagination via `?n=<count>&last=<last-tag>`.

## Catalog / Referrers

### List Repositories (Catalog)

```
GET /v2/_catalog
```

Response:

```json
{
  "repositories": ["repo1", "repo2"]
}
```

Supports pagination via `?n=<count>&last=<last-repo>`.

### List Referrers

```
GET /v2/<name>/referrers/<digest>?artifactType=<type>
```

Response: OCI Image Index containing descriptors of referring artifacts.

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:...",
      "size": 1234,
      "artifactType": "application/vnd.example.sbom.v1",
      "annotations": {}
    }
  ]
}
```

## Common Headers

| Header | Direction | Description |
|--------|-----------|-------------|
| `Authorization` | Request | `Basic <base64>` or `Bearer <token>` |
| `Content-Type` | Request | Media type of the body |
| `Accept` | Request | Accepted media types for response |
| `Docker-Content-Digest` | Response | Digest of the content |
| `Content-Length` | Both | Size in bytes |
| `Location` | Response | URL for created/uploaded resources |
| `Link` | Response | Pagination (`</v2/...?last=...&n=...>; rel="next"`) |
| `OCI-Filters-Applied` | Response | Referrers filtering applied |
| `Range` | Response | Byte range for chunked uploads |

## Error Response Format

```json
{
  "errors": [
    {
      "code": "MANIFEST_UNKNOWN",
      "message": "manifest unknown",
      "detail": {}
    }
  ]
}
```

Common error codes:

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `BLOB_UNKNOWN` | 404 | Blob not found |
| `BLOB_UPLOAD_INVALID` | 400 | Invalid upload |
| `BLOB_UPLOAD_UNKNOWN` | 404 | Upload session not found |
| `DIGEST_INVALID` | 400 | Provided digest is invalid |
| `MANIFEST_BLOB_UNKNOWN` | 404 | Blob referenced by manifest not found |
| `MANIFEST_INVALID` | 400 | Manifest is invalid |
| `MANIFEST_UNKNOWN` | 404 | Manifest not found |
| `NAME_INVALID` | 400 | Invalid repository name |
| `NAME_UNKNOWN` | 404 | Repository not found |
| `SIZE_INVALID` | 400 | Size mismatch |
| `UNAUTHORIZED` | 401 | Authentication required |
| `DENIED` | 403 | Access denied |
| `UNSUPPORTED` | 405 | Operation not supported |
| `TOOMANYREQUESTS` | 429 | Rate limited |

## OCI Media Types

| Media Type | Description |
|------------|-------------|
| `application/vnd.oci.image.manifest.v1+json` | OCI Image Manifest |
| `application/vnd.oci.image.index.v1+json` | OCI Image Index (multi-arch) |
| `application/vnd.oci.image.config.v1+json` | OCI Image Configuration |
| `application/vnd.oci.image.layer.v1.tar` | Uncompressed layer |
| `application/vnd.oci.image.layer.v1.tar+gzip` | Gzip compressed layer |
| `application/vnd.oci.image.layer.v1.tar+zstd` | Zstd compressed layer |
| `application/vnd.oci.empty.v1+json` | Empty descriptor (for artifacts) |
| `application/vnd.docker.distribution.manifest.v2+json` | Docker V2 Manifest |
| `application/vnd.docker.distribution.manifest.list.v2+json` | Docker Manifest List |

## OCI Image Manifest Schema

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "<optional media type>",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:...",
    "size": 1234
  },
  "layers": [
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:...",
      "size": 5678,
      "annotations": {}
    }
  ],
  "subject": {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "digest": "sha256:...",
    "size": 1234
  },
  "annotations": {}
}
```

## OCI Image Index Schema

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:...",
      "size": 1234,
      "platform": {
        "architecture": "amd64",
        "os": "linux"
      }
    }
  ],
  "annotations": {}
}
```
