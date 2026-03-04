---
name: regshape-dev
description: "Design, implement, and document the RegShape Python library and CLI for OCI (Open Containers Initiative) registry interactions. Use when: (1) Designing API specs or data models for registry operations, (2) Implementing Python library modules for manifests, blobs, tags, auth, or referrers, (3) Designing CLI commands using Click, (4) Writing user documentation for the library or CLI, (5) Planning break/test mode features for testing registry implementations, (6) Working on any RegShape library or CLI code."
---

# RegShape Development

Design, implement, and document a Python library and CLI for interacting with OCI registries.

## Workflow

The recommended workflow is **spec → implement → test → document**, but any phase can be entered directly.

### Phase 1: Design Spec

1. Read [references/oci-distribution-spec.md](references/oci-distribution-spec.md) for the relevant OCI API details
2. Read [references/spec-templates.md](references/spec-templates.md) for the spec template to use
3. Read [references/project-conventions.md](references/project-conventions.md) for naming and structure patterns
4. Create the spec as a Markdown file in `/specs/<domain>/<feature>.md`
5. Present the spec to the user for review before proceeding

### Phase 2: Implement

1. Read the relevant spec from `/specs/`
2. Read [references/project-conventions.md](references/project-conventions.md) for code patterns
3. Implement in `src/regshape/libs/<domain>/` following existing patterns:
   - Module file with functions/classes
   - Update `__init__.py` exports
   - Add error types to `libs/errors.py` if needed
4. Write tests in `src/regshape/tests/test_<module>.py`
5. Run `pytest` to verify

### Phase 3: CLI Design & Implementation

1. Read [references/spec-templates.md](references/spec-templates.md) for the CLI command spec template
2. Design the command structure and save spec to `/specs/cli/<command>.md`
3. Implement commands in `src/regshape/cli/` using Click
4. Wire commands to library functions from `libs/`
5. Follow CLI conventions from [references/project-conventions.md](references/project-conventions.md)

### Phase 4: Document

1. Read [references/doc-templates.md](references/doc-templates.md) for documentation templates
2. Write user docs in `/docs/` following the folder structure:
   - `docs/library/` — Library API reference
   - `docs/cli/` — CLI command reference
   - `docs/guides/` — How-to guides
3. Keep docs aligned with the specs and implementation

## Domain Areas

| Domain | Library Path | Spec Path | Doc Path |
|--------|-------------|-----------|----------|
| Authentication | `libs/auth/` | `specs/auth/` | `docs/library/auth.md` |
| Manifests | `libs/manifests/` | `specs/registry/manifest-operations.md` | `docs/library/manifests.md` |
| Blobs | `libs/blobs/` | `specs/registry/blob-operations.md` | `docs/library/blobs.md` |
| Tags | `libs/tags/` | `specs/registry/tag-operations.md` | `docs/library/tags.md` |
| Referrers | `libs/referrers/` | `specs/registry/referrers.md` | `docs/library/referrers.md` |
| Catalog | `libs/catalog/` | `specs/registry/catalog.md` | `docs/library/catalog.md` |
| Break Mode | `libs/breakmode/` | `specs/breakmode/` | `docs/guides/break-mode.md` |
| CLI | `cli/` | `specs/cli/` | `docs/cli/` |
| Models | `libs/models/` | `specs/models/` | `docs/library/models.md` |

## GitHub Issue Management

Use the GitHub CLI (`gh`) to create and manage development tracking issues directly from the skill.

### Creating Issues

When identifying work to be done, create GitHub issues using:

```bash
gh issue create \
  --title "Issue Title" \
  --body "Issue description with acceptance criteria" \
  --label "enhancement" \
  --label "priority-high" \
  --assignee @me
```

### Common Issue Labels

- **Priority**: `priority-high`, `priority-medium`, `priority-low`  
- **Type**: `enhancement`, `bug`, `documentation`, `architecture`
- **Domain**: `break-mode`, `cli`, `oci-spec`, `transport`, `models`
- **Status**: `blocked`, `help-wanted`, `good-first-issue`

### Issue Templates

Use these templates for consistent issue creation:

**Feature Implementation Issues:**
- Title: `Implement [Feature Name] - [Brief Description]`
- Include acceptance criteria checklist
- List files to create/modify
- Add appropriate priority and domain labels

**Bug Report Issues:**
- Title: `Bug: [Brief Description]`  
- Include steps to reproduce, expected vs actual behavior
- Add `bug` label and relevant domain labels

**Architecture Issues:**
- Title: `Architecture: [Component/Enhancement]`
- Include design decisions, trade-offs, implementation notes
- Add `architecture` label

### Workflow Integration

1. **When planning work**: Create issues for each major component or feature
2. **Before coding**: Reference issue numbers in commit messages (`fixes #123`)  
3. **During implementation**: Update issue status and add progress notes
4. **After completion**: Close issues automatically via commit messages or PR merges

## Break/Test Mode

RegShape's distinguishing feature: deliberately send malformed or non-conformant requests to test registry implementations.

When designing break mode features:
- Provide options to modify any part of a request (headers, body, method, path, digest values)
- Allow sending requests with invalid content types, wrong digests, oversized payloads
- Support skipping authentication steps or using expired tokens
- Log full request/response pairs for analysis
- Make break mode explicit and opt-in (never active by default)

## CLI Design Principles

### Auth-First Authentication

Authentication is always performed as a separate, explicit step via `regshape auth login`.
All other command groups (manifest, blob, tag, referrer, catalog) resolve credentials
automatically from the Docker credential store and **do not** accept per-command
`--username` / `--password` flags.

- The `auth login` command stores credentials using the Docker credential store mechanics
  (`credHelpers` in `~/.docker/config.json` or base64-encoded `auths` entries).
- Non-auth commands call `resolve_credentials(registry, None, None)` to look up stored
  credentials for the embedded registry.
- If no stored credentials exist and the registry requires authentication, the registry
  returns HTTP 401, which surfaces as exit code 1.

### Registry Must Be Embedded in Image Reference

The `--image-ref` / `-i` flag always requires the registry to be embedded:

```
registry/repository:tag         # e.g., acr.io/myrepo/myimage:latest
registry/repository@sha256:...  # e.g., acr.io/myrepo/myimage@sha256:abc...
```

There is no global or per-command `--registry` flag. The registry is extracted from
the image reference by `_parse_image_ref(image_ref)`.

### Global Options

The root `regshape` group only provides infrastructure-level options:
`--insecure`, `--verbose`, `--break`, `--break-rules`, `--log-file`.
There are no global auth options.

## Key References

- **OCI Distribution Spec**: [references/oci-distribution-spec.md](references/oci-distribution-spec.md) — API endpoints, schemas, error codes
- **Project Conventions**: [references/project-conventions.md](references/project-conventions.md) — Code style, module structure, naming
- **Spec Templates**: [references/spec-templates.md](references/spec-templates.md) — Templates for `/specs` documents
- **Doc Templates**: [references/doc-templates.md](references/doc-templates.md) — Templates for `/docs` documents
