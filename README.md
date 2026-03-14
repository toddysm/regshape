# RegShape

![GitHub issues](https://img.shields.io/github/issues-raw/toddysm/regshape?link=https%3A%2F%2Fgithub.com%2Ftoddysm%2Fregshape%2Fissues)
 ![GitHub pull requests](https://img.shields.io/github/issues-pr-raw/toddysm/regshape?link=https%3A%2F%2Fgithub.com%2Ftoddysm%2Fregshape%2Fpulls)

<p align="center">
  <img src="./docs/media/regshape-logo.png" alt="RegShape" width="256">
</p>

RegShape (from **REG**istry re**SHAPE**) is a CLI tool and a Python library for
manipulating artifacts in an [OCI](https://opencontainers.org) registry. While
there are many other tools that can do this (see
[ORAS](https://oras.land),
[regclient](https://github.com/regclient/regclient), or Google's
[crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)),
the goal of RegShape is to provide flexibility to manipulate the requests with
an intention to break the consistency of the artifacts.

You can use RegShape in two modes:

- **Standard mode** — interact with registries as you would with any other tool:
  pull and push manifests, blobs, tags, and more.
- **Expert / break mode** — manually craft requests to test registry
  implementations and probe their security boundaries.

RegShape is written in Python and offers Python libraries that can be leveraged
to build your own tools. The CLI is built on top of the libraries and uses the
[Click](https://click.palletsprojects.com/) framework.

> **Note:** The tool is still in early development and the API is not stable yet.

## Installation

```bash
git clone https://github.com/toddysm/regshape.git
cd regshape
pip install -e .
```

## Quick Start

Ping a registry to verify connectivity:

```bash
regshape ping registry-1.docker.io
```

Retrieve a manifest:

```bash
regshape manifest get -i docker.io/library/alpine:latest
```

List tags for a repository:

```bash
regshape tag list -i docker.io/library/alpine
```

## Documentation

### Guides

- [Creating OCI Layouts](docs/guides/create-oci-layout.md)
- [Docker Desktop Integration](docs/guides/docker-desktop-integration.md)

### Architecture & Design

- [Architecture Overview](specs/architecture.md)

### CLI Command Specs

| Command | Spec |
|---------|------|
| Auth | [specs/cli/auth.md](specs/cli/auth.md) |
| Blob | [specs/cli/blob.md](specs/cli/blob.md) |
| Catalog | [specs/cli/catalog.md](specs/cli/catalog.md) |
| Manifest | [specs/cli/manifest.md](specs/cli/manifest.md) |
| Tag | [specs/cli/tag.md](specs/cli/tag.md) |
| Referrer | [specs/cli/referrer.md](specs/cli/referrer.md) |
| Ping | [specs/cli/ping.md](specs/cli/ping.md) |
| Layout | [specs/cli/layout.md](specs/cli/layout.md) |
| Layout Push | [specs/cli/layout-push.md](specs/cli/layout-push.md) |
| Formatting | [specs/cli/formatting.md](specs/cli/formatting.md) |

### Library Specs

- [Models](specs/models/) — Blob, Catalog, Error, Manifest, Referrer, Tags
- [Operations](specs/operations/) — Blobs, Catalog, Manifests, Referrers, Tags

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for instructions on setting up your
development environment.

## Security

To report a vulnerability, please see [SECURITY.md](SECURITY.md).

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.