# Spec Templates

Templates for design specifications saved to the `/specs` folder. Each spec is a Markdown file.

## Table of Contents

- [Folder Structure](#folder-structure)
- [Module Spec Template](#module-spec-template)
- [CLI Command Spec Template](#cli-command-spec-template)
- [Data Model Spec Template](#data-model-spec-template)

## Folder Structure

Organize specs by domain area:

```
specs/
├── README.md
├── auth/
│   ├── authentication-flow.md
│   └── credential-stores.md
├── registry/
│   ├── manifest-operations.md
│   ├── blob-operations.md
│   └── tag-operations.md
├── cli/
│   ├── command-structure.md
│   └── output-formats.md
└── models/
    ├── manifest.md
    └── descriptor.md
```

## Module Spec Template

Use for library modules that implement registry operations.

```markdown
# <Module Name>

## Overview

Brief description of what this module does and why it exists.

## API Surface

### Functions

#### `function_name(param1: type, param2: type) -> return_type`

Description of the function.

**Parameters:**
- `param1` — Description
- `param2` — Description

**Returns:** Description

**Raises:**
- `ErrorType` — When condition

**Example:**
\```python
result = function_name("value1", "value2")
\```

### Classes

#### `ClassName`

Description of the class.

**Attributes:**
- `attr1: type` — Description

**Methods:**
- `method_name(params) -> return_type` — Description

## Data Models

Describe any data structures (dataclasses, TypedDicts, etc.) this module defines or uses.

## Protocol Flow

Describe the network protocol interactions (request/response sequences) if applicable.

\```
Client                          Registry
  |                                |
  |--- GET /v2/<name>/...  ------->|
  |<-- 200 OK + body -------------|
\```

## Error Handling

Describe error cases and how they are handled.

| Condition | Error Type | Behavior |
|-----------|-----------|----------|
| ... | ... | ... |

## Dependencies

- Internal: modules this depends on
- External: third-party packages used

## Open Questions

- [ ] Question about design decision?
```

## CLI Command Spec Template

Use for specifying CLI commands and subcommands.

```markdown
# CLI: `<command-name>`

## Overview

What this command does.

## Usage

\```
regshape <command> [OPTIONS] <ARGS>
\```

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `<arg>` | Yes | Description |

## Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--option` | `-o` | string | none | Description |
| `--json` | | flag | false | Output as JSON |
| `--verbose` | `-v` | flag | false | Verbose output |

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `sub1` | Description |
| `sub2` | Description |

## Examples

\```bash
# Example 1: Basic usage
regshape <command> arg1

# Example 2: With options
regshape <command> --option value arg1
\```

## Output Format

### Plain text (default)

\```
Expected output
\```

### JSON (`--json`)

\```json
{
  "key": "value"
}
\```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |

## Error Messages

| Scenario | Message |
|----------|---------|
| ... | ... |
```

## Data Model Spec Template

Use for specifying data models and schemas.

```markdown
# Data Model: `<ModelName>`

## Overview

What this model represents.

## Schema

\```python
@dataclass
class ModelName:
    field1: str        # Description
    field2: int        # Description
    field3: Optional[str] = None  # Description
\```

## JSON Representation

\```json
{
  "field1": "value",
  "field2": 123,
  "field3": null
}
\```

## Validation Rules

| Field | Rule | Error |
|-------|------|-------|
| `field1` | Must not be empty | ValueError |

## Relationships

Describe how this model relates to other models.

## Serialization

How this model is serialized/deserialized (JSON, wire format, etc.).
```
