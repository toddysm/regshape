# Contributing to RegShape

## Setting Up Your Environment

### Setting Up Visual Studio Code
Create an `.env` file in the root directory of the project and add the following:
```
PYTHONPATH=<your_workspace_folder_path>/src
```

## Testing

Install the package and its test dependencies, then run the test suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The [test workflow](.github/workflows/tests.yml) runs the suite on Python 3.10,
3.11, and 3.12 for pull requests and pushes to `main`. Tests include mocked
registry interactions; they do not require a live registry or Docker daemon.

## Releasing

### Version Tagging Convention

Release tags follow the format `vX.Y.Z` (e.g., `v0.1.0`, `v1.0.0`). The version
in the tag **must** match the `version` field in `pyproject.toml`.

### How to Create a Release

1. Update the `version` field in `pyproject.toml` to the new version.
2. Commit the version bump: `git commit -am "Bump version to X.Y.Z"`.
3. Push the commit to `main`.
4. Create a GitHub Release:
   - Go to **Releases** → **Draft a new release**.
   - Create a new tag `vX.Y.Z` targeting `main`.
   - Add release notes describing the changes.
   - Click **Publish release**.
5. The `publish.yml` workflow runs automatically, building and publishing the
   package to PyPI.

### What the Workflow Does

The `publish.yml` GitHub Actions workflow:

1. **Build** — Checks out the code, validates the tag version matches
   `pyproject.toml`, builds sdist and wheel distributions, and verifies them
   with `twine check`.
2. **Publish** — Downloads the build artifacts and publishes to PyPI using
   trusted publishing (OIDC).

### Verifying the Published Package

After the workflow completes, verify the package at
https://pypi.org/project/regshape/. You can also install it with:

```
pip install regshape==X.Y.Z
```

### Trusted Publishing Setup (One-Time)

PyPI trusted publishing must be configured once by the repository owner:

1. On [PyPI](https://pypi.org/), go to **Account** → **Publishing** → **Add a
   new pending publisher**.
2. Fill in: Owner = `toddysm`, Repository = `regshape`, Workflow = `publish.yml`,
   Environment = `pypi`.
3. On GitHub, create an environment named `pypi` under **Settings** →
   **Environments**. Optionally add required reviewers for an approval gate.