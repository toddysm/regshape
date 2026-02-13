# Documentation Templates

Templates for user-facing documentation saved to the `/docs` folder.

## Table of Contents

- [Folder Structure](#folder-structure)
- [Getting Started Guide](#getting-started-guide)
- [Library API Reference](#library-api-reference)
- [CLI Reference](#cli-reference)
- [How-To Guide](#how-to-guide)

## Folder Structure

```
docs/
├── index.md                # Project overview and navigation
├── getting-started.md      # Installation and quickstart
├── library/
│   ├── overview.md         # Library architecture overview
│   ├── auth.md             # Authentication module docs
│   ├── manifests.md        # Manifest operations docs
│   ├── blobs.md            # Blob operations docs
│   └── ...
├── cli/
│   ├── overview.md         # CLI overview and global options
│   ├── auth.md             # Auth commands
│   ├── manifests.md        # Manifest commands
│   └── ...
└── guides/
    ├── testing-registries.md
    └── break-mode.md
```

## Getting Started Guide

```markdown
# Getting Started

## Installation

\```bash
pip install regshape
\```

## Quick Start

### As a Library

\```python
from regshape.libs.auth import registryauth

# Authenticate with a registry
token = registryauth.authenticate(auth_header, username, password)
\```

### As a CLI

\```bash
# Check registry connectivity
regshape ping registry.example.com

# List tags for a repository
regshape tags list registry.example.com/myrepo
\```

## What's Next

- [Library API Reference](library/overview.md)
- [CLI Reference](cli/overview.md)
- [How-To Guides](guides/)
```

## Library API Reference

```markdown
# <Module Name>

## Overview

Brief description of the module's purpose.

## Functions

### `function_name`

\```python
function_name(param1: str, param2: int = 0) -> str
\```

Description of what the function does.

**Parameters:**
- `param1` (str): Description
- `param2` (int, optional): Description. Defaults to `0`.

**Returns:** `str` — Description

**Raises:**
- `AuthError`: When authentication fails

**Example:**
\```python
result = function_name("value")
print(result)
\```

## Classes

### `ClassName`

Description.

#### Constructor

\```python
ClassName(param1: str, param2: int)
\```

#### Methods

##### `method_name`

\```python
method_name(param: str) -> bool
\```

Description.

## Exceptions

### `ErrorName`

Raised when condition occurs.
```

## CLI Reference

```markdown
# `regshape <command>`

Description of the command.

## Usage

\```bash
regshape <command> [OPTIONS] ARGS
\```

## Options

| Option | Description |
|--------|-------------|
| `--option` | Description |

## Examples

\```bash
# Example with description
regshape <command> --option value arg
\```

## See Also

- [Related command](related.md)
```

## How-To Guide

```markdown
# How to <Task>

## Prerequisites

- Requirement 1
- Requirement 2

## Steps

### 1. First Step

Description and code example.

\```python
# code
\```

### 2. Second Step

Description and code example.

## Troubleshooting

### Problem: Description

**Solution:** Steps to fix.

## Related

- [Link to related doc](path.md)
```
