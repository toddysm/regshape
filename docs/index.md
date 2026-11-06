# RegShape Overview

<!-- doc-type: overview -->

![RegShape](media/regshape-logo.png)

RegShape (from REGistry reSHAPE) is a command-line interface (CLI) tool and a Python library for manipulating
artifacts in an [Open Container Initiative (OCI)](https://opencontainers.org) registry. While there are many other tools that can do this
(see [ORAS](https://oras.land), [regclient](https://github.com/regclient/regclient)
or Google's [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)),
the goal of RegShape is to provide flexibility to manipulate the requests with
an intention to break the consistency of the artifacts.

You should be able to use RegShape similarly to the tools above, but you can also
use it in expert mode where you can manually craft the requests to the registry
and try to break it. This is useful for testing the registry implementations or
the security of the registry.

RegShape is written in Python and offers Python libraries that can be leveraged
to build your own tools. The CLI tool is built on top of the libraries and uses
[click](https://click.palletsprojects.com/) framework.

## Guides and references

- [Install and get started](../README.md#installation)
- [Download, verify, and republish a complete artifact](guides/artifact-pull.md)
- [Artifact pull command reference](../specs/cli/artifact.md)
- [Complete-artifact library reference](../specs/operations/artifacts.md)
- [Recursive layout push reference](../specs/cli/layout-push.md)
- [Create an OCI layout](guides/create-oci-layout.md)
- [Docker Desktop integration](guides/docker-desktop-integration.md)
- [Monitor live transfer progress](guides/live-transfer-progress.md)
- [Download a manifest or index with progress](guides/live-transfer-progress.md#download-a-manifest-or-index)
- [Library transfer callbacks](library/transfer-progress.md)
- [Manifest receive observations](library/transfer-progress.md#manifest-receive-observations)
- [Artifact options and legacy-flag migration](guides/artifact-options.md)
- [Implemented capabilities and remaining work](project-state.md)

Complete-artifact workflows describe development builds targeting 0.2, not
release availability. **Linux/macOS and compatible native filesystems are
required; Windows fails closed.** Read the
[private-namespace prerequisites](guides/artifact-pull.md#before-you-start-supported-storage-and-trust)
before using pull, recursive validation, or push.
