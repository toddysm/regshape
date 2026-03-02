# Operations: Tags

## Overview

This spec documents the domain operations layer for OCI tag interactions
in `src/regshape/libs/tags/operations.py`.

The tags operations layer sits between the CLI and the foundation layer.
It never calls `requests` directly — all HTTP traffic goes through a
`RegistryClient` instance provided by the caller.

### Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| List tags | `GET` | `/v2/{repo}/tags/list[?n=<n>&last=<last>]` |
| Delete tag | `DELETE` | `/v2/{repo}/manifests/{tag}` |

---

## Module Structure

```
src/regshape/libs/tags/
├── __init__.py        # Package init; re-exports public symbols
└── operations.py      # Public domain operations + private error helpers
```

---

## Public Operations

### `list_tags`

```python
@track_time
def list_tags(
    client: RegistryClient,
    repo: str,
    page_size: Optional[int] = None,
    last: Optional[str] = None,
) -> TagList:
```

Fetches the tag list for a repository (single page).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name (e.g. `"myrepo/myimage"`) |
| `page_size` | `Optional[int]` | `None` | OCI `n` query parameter; omitted when `None` |
| `last` | `Optional[str]` | `None` | Lexicographic cursor for pagination; omitted when `None` |

**Returns:** `TagList`

**Behaviour:**

1. Build path `"/v2/{repo}/tags/list"`.
2. Append `n=<page_size>` and/or `last=<last>` when provided.
3. Call `client.get(path, params=params)`.
4. Pass response to `_raise_for_list_error`.
5. Deserialize and return `TagList.from_json(response.text)`.
   Re-raises `TagError` directly; wraps all other exceptions in `TagError`.

**Decorator:** `@track_time`

---

### `delete_tag`

```python
@track_time
def delete_tag(
    client: RegistryClient,
    repo: str,
    tag: str,
) -> None:
```

Deletes a tag from a repository. Per the OCI Distribution Spec, tag deletion
is routed through the manifests endpoint.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `client` | `RegistryClient` | required | Authenticated transport client |
| `repo` | `str` | required | Repository name |
| `tag` | `str` | required | Tag name to delete (must not be a digest) |

**Returns:** `None`

**Behaviour:**

1. Build path `"/v2/{repo}/manifests/{tag}"`.
2. Call `client.delete(path)`.
3. Pass response to `_raise_for_delete_error`.

**Decorator:** `@track_time`

---

## Private Helpers

### `_raise_for_list_error`

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `TagError` | `"Repository not found: {registry}/{repo}"` |
| other non-2xx | `TagError` | `"Registry error for {registry}/{repo}: HTTP {status}"` |

### `_raise_for_delete_error`

| HTTP status | Exception | Message |
|---|---|---|
| `401` | `AuthError` | `"Authentication failed for {registry}"` |
| `404` | `TagError` | `"Tag not found: {ref}"` |
| `400`, `405` | `TagError` | `"Tag deletion is not supported by this registry"` |
| other non-2xx | `TagError` | `"Registry error for {ref}: HTTP {status}"` |

Both helpers populate the `detail` field from
`OciErrorResponse.from_response(response).first_detail()`, falling back to
the first 200 characters of `response.text`.

---

## Telemetry

| Decorator | Applied to |
|---|---|
| `@track_time` | `list_tags`, `delete_tag` |

No `@track_scenario` is needed — neither operation is multi-step at the HTTP
level.

---

## Error Handling Summary

| Condition | Exception | Raised by |
|---|---|---|
| `401` on any call | `AuthError` | `_raise_for_list_error` / `_raise_for_delete_error` |
| `404` on list | `TagError` "Repository not found" | `_raise_for_list_error` |
| `404` on delete | `TagError` "Tag not found" | `_raise_for_delete_error` |
| `400` / `405` on delete | `TagError` "not supported" | `_raise_for_delete_error` |
| Malformed JSON body | `TagError` | `TagList.from_json` (re-raised) |
| Transport / connection failure | `requests.exceptions.RequestException` | propagated as-is |

---

## Dependencies

**Internal:**
- `regshape.libs.errors` — `AuthError`, `TagError`
- `regshape.libs.models.tags` — `TagList`
- `regshape.libs.models.error` — `OciErrorResponse`
- `regshape.libs.refs` — `format_ref`
- `regshape.libs.transport` — `RegistryClient`
- `regshape.libs.decorators.timing` — `track_time`

**External:**
- `requests` — `requests.Response` type annotation only
