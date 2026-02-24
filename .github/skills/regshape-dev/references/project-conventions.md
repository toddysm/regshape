# RegShape Project Conventions

Conventions and patterns observed in the existing RegShape codebase. Follow these when implementing new modules.

## Table of Contents

- [Project Structure](#project-structure)
- [Module Organization](#module-organization)
- [Naming Conventions](#naming-conventions)
- [Code Style](#code-style)
- [Error Handling](#error-handling)
- [Dependencies](#dependencies)
- [Testing](#testing)
- [CLI Conventions](#cli-conventions)

## Project Structure

```
src/regshape/
├── __init__.py           # Package root
├── cli/                  # CLI commands (Click-based)
├── libs/                 # Core library modules
│   ├── __init__.py       # Exports submodule names
│   ├── auth/             # Auth subpackage
│   ├── constants.py      # Shared constants
│   ├── decorators/       # Cross-cutting decorators
│   └── errors.py         # Custom exception hierarchy
└── tests/                # Test suite
    ├── __init__.py
    └── test_<module>.py
```

### Key Directories

- `src/regshape/libs/` — All library code goes here, organized by domain
- `src/regshape/cli/` — CLI commands using Click framework
- `src/regshape/tests/` — Tests using pytest, inside the package
- `specs/` — Design specifications (Markdown)
- `docs/` — User-facing documentation

## Module Organization

### Subpackage Pattern

Each domain area is a subpackage under `libs/`:

```
libs/<domain>/
├── __init__.py    # Exports submodule names as strings
├── module1.py
└── module2.py
```

The `__init__.py` exports submodule names:

```python
__all__ = ['module1', 'module2']
```

### Top-level `libs/__init__.py`

Exports subpackage names:

```python
__all__ = ['constants', 'errors', 'auth', 'decorators']
```

## Naming Conventions

- **Modules**: `snake_case.py` (e.g., `registryauth.py`, `dockerconfig.py`)
- **Functions**: `snake_case` with leading underscore for internal helpers (e.g., `_parse_auth_header`, `authenticate`)
- **Classes**: `PascalCase` (e.g., `RegShapeError`, `AuthError`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `IS_WINDOWS_PLATFORM`, `TRACK_EXECUTION_TIME`)
- **Test files**: `test_<module>.py` (e.g., `test_auth.py`)

## Code Style

### Docstrings

Use Sphinx-style docstrings with type annotations:

```python
def function_name(param1: str, param2: int = 0) -> str:
    """Brief description of the function.

    Longer description if needed.

    :param param1: Description of param1.
    :param param2: Description of param2.
    :returns: Description of return value.
    :raises SomeError: When something goes wrong.
    """
```

### Type Hints

Use type hints on all public function signatures:

```python
def authenticate(auth_header: str, username: str, password: str) -> str:
```

### Imports

- Standard library first, then third-party, then local
- Use absolute imports from `regshape` package

```python
import json
import subprocess

import requests

from regshape.libs.errors import AuthError
from regshape.libs.decorators.telemetry import executiontime_decorator
```

### Logging

Use the `logging` module with module-level logger:

```python
import logging

logger = logging.getLogger(__name__)
```

### Platform Awareness

Check platform when needed:

```python
from regshape.libs.constants import IS_WINDOWS_PLATFORM
```

## Error Handling

### Exception Hierarchy

```
RegShapeError (base)
├── AuthError
└── <new error types extend RegShapeError>
```

### Pattern

- Define custom exceptions in `libs/errors.py`
- Inherit from `RegShapeError`
- Raise domain-specific errors with descriptive messages
- Catch and re-raise as appropriate error types at boundaries

```python
class RegistryError(RegShapeError):
    """Raised when a registry operation fails."""
    pass
```

## Dependencies

Current dependencies (from `requirements.txt`):

- `click>=8.1.0` — CLI framework
- `pytest>=7.4.0` — Testing
- `requests>=2.31.0` — HTTP client

### Adding Dependencies

- Add to `requirements.txt` with minimum version pinning (`>=`)
- Prefer well-maintained, widely-used packages
- Keep dependencies minimal

## Testing

- Framework: `pytest`
- Test location: `src/regshape/tests/`
- Config: `pytest.ini` sets `pythonpath = src`
- Run: `pytest` from project root
- Name test files: `test_<module>.py`
- Name test functions: `test_<behavior>()`

## CLI Conventions

The CLI uses the Click framework. When implementing:

- Define command groups in `src/regshape/cli/`
- Use Click decorators (`@click.command()`, `@click.group()`, `@click.option()`)
- Follow Click patterns for help text, argument validation, and error output
- Output format: plain text by default, `--json` flag for machine-readable output
- Use `click.echo()` for output, `click.secho()` for styled output
- Exit codes: 0 for success, 1 for errors
