#!/usr/bin/env python3

"""
:mod:`regshape.cli.blob` - CLI commands for OCI blob operations
===============================================================

.. module:: regshape.cli.blob
   :platform: Unix, Windows
   :synopsis: Click command group providing head, get, delete, upload,
              and mount subcommands for OCI blob operations.

.. moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import json
import sys
from typing import Optional

import click
import requests

from regshape.libs.blobs import (
    delete_blob,
    get_blob,
    head_blob,
    mount_blob,
    upload_blob,
    upload_blob_chunked,
)
from regshape.libs.decorators import telemetry_options
from regshape.libs.decorators.scenario import track_scenario
from regshape.libs.errors import AuthError, BlobError
from regshape.libs.refs import parse_image_ref
from regshape.libs.transport import RegistryClient, TransportConfig


# ===========================================================================
# Public Click group
# ===========================================================================


@click.group()
def blob():
    """Manage OCI blobs (head, get, delete, upload, mount)."""
    pass


# ===========================================================================
# blob head
# ===========================================================================


@blob.command("head")
@telemetry_options
@click.option(
    "--repo",
    "-r",
    required=True,
    metavar="REPO",
    help="Repository in 'registry/name' format (e.g. registry.io/myrepo).",
)
@click.option(
    "--digest",
    "-d",
    required=True,
    metavar="DIGEST",
    help="Blob digest in 'algorithm:hex' format (e.g. sha256:abc...).",
)
@click.pass_context
@track_scenario("blob head")
def blob_head(ctx, repo, digest):
    """Check blob existence and retrieve metadata without downloading content.

    Issues a HEAD request to the registry for the blob identified by REPO
    and DIGEST.  Credentials are resolved automatically from the credential
    store populated by ``auth login``.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo_name, _ = parse_image_ref(repo)
    except ValueError as exc:
        _error(repo, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        info = head_blob(client=client, repo=repo_name, digest=digest)
    except (AuthError, BlobError, requests.exceptions.RequestException) as exc:
        _error(f"{repo}@{digest}", str(exc))
        sys.exit(1)

    click.echo(json.dumps(info.to_dict(), indent=2))


# ===========================================================================
# blob get
# ===========================================================================


@blob.command("get")
@telemetry_options
@click.option(
    "--repo",
    "-r",
    required=True,
    metavar="REPO",
    help="Repository in 'registry/name' format.",
)
@click.option(
    "--digest",
    "-d",
    required=True,
    metavar="DIGEST",
    help="Blob digest (e.g. sha256:abc...).",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    metavar="PATH",
    help="File path to write blob content to. Omit to verify without saving.",
)
@click.option(
    "--chunk-size",
    type=int,
    default=65536,
    show_default=True,
    metavar="BYTES",
    help="Streaming chunk size in bytes.",
)
@click.pass_context
@track_scenario("blob get")
def blob_get(ctx, repo, digest, output, chunk_size):
    """Download a blob and verify its digest.

    The blob content is streamed from the registry and the SHA-256 digest is
    verified against DIGEST. When --output is supplied the streamed content
    is written to the specified file path; otherwise it is not saved locally.

    The --chunk-size option controls the size of streaming chunks used when
    downloading the blob. Credentials are resolved automatically.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo_name, _ = parse_image_ref(repo)
        # blob_get expects --repo to be a plain "registry/repository" without any tag
        # (":tag") or digest ("@sha256:...") suffix. Reject non-plain refs explicitly
        # so behavior matches the documented CLI contract.
        # Compare the reconstructed plain form against the raw input: any qualifier
        # (including ":latest") will cause a mismatch.
        if repo.rstrip("/") != f"{registry}/{repo_name}":
            _error(
                repo,
                "--repo must be a plain 'registry/repository' without tag or digest "
                "(e.g. ':tag' or '@sha256:...')",
            )
            sys.exit(1)
    except ValueError as exc:
        _error(repo, str(exc))
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        info = get_blob(
            client=client,
            repo=repo_name,
            digest=digest,
            output_path=output,
            chunk_size=chunk_size,
        )
    except (AuthError, BlobError, requests.exceptions.RequestException) as exc:
        _error(f"{repo}@{digest}", str(exc))
        sys.exit(1)

    click.echo(json.dumps(info.to_dict(), indent=2))


# ===========================================================================
# blob delete
# ===========================================================================


@blob.command("delete")
@telemetry_options
@click.option(
    "--repo",
    "-r",
    required=True,
    metavar="REPO",
    help="Repository in 'registry/name' format.",
)
@click.option(
    "--digest",
    "-d",
    required=True,
    metavar="DIGEST",
    help="Blob digest to delete (e.g. sha256:abc...).",
)
@click.pass_context
@track_scenario("blob delete")
def blob_delete(ctx, repo, digest):
    """Delete a blob from the registry.

    Issues DELETE to the registry for the blob identified by REPO and DIGEST.
    The registry returns 202 Accepted on success.

    Warning: deleting a blob may break manifests that reference it if the
    registry does not enforce referential integrity.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo_name, _ = parse_image_ref(repo)
    except ValueError as exc:
        _error(repo, str(exc))
        sys.exit(1)

    if repo.rstrip("/") != f"{registry}/{repo_name}":
        _error(repo, "--repo must be a plain 'registry/repository' without tag or digest "
               "(e.g. ':tag' or '@sha256:...')")
        sys.exit(1)

    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        delete_blob(client=client, repo=repo_name, digest=digest)
    except (AuthError, BlobError, requests.exceptions.RequestException) as exc:
        _error(f"{repo}@{digest}", str(exc))
        sys.exit(1)

    click.echo(json.dumps({"digest": digest, "status": "deleted"}, indent=2))


# ===========================================================================
# blob upload
# ===========================================================================


@blob.command("upload")
@telemetry_options
@click.option(
    "--repo",
    "-r",
    required=True,
    metavar="REPO",
    help="Repository in 'registry/name' format.",
)
@click.option(
    "--file",
    "-f",
    "source_file",
    required=True,
    type=click.Path(exists=True),
    metavar="FILE",
    help="Local file to upload.",
)
@click.option(
    "--digest",
    "-d",
    required=True,
    metavar="DIGEST",
    help="Expected digest of the blob (e.g. sha256:abc...).",
)
@click.option(
    "--media-type",
    default="application/octet-stream",
    show_default=True,
    metavar="MIMETYPE",
    help="Content-Type for the blob.",
)
@click.option(
    "--chunked",
    is_flag=True,
    default=False,
    help="Use chunked (streaming) upload protocol instead of monolithic.",
)
@click.option(
    "--chunk-size",
    type=int,
    default=65536,
    show_default=True,
    metavar="BYTES",
    help="Chunk size in bytes (chunked mode only).",
)
@click.pass_context
@track_scenario("blob upload")
def blob_upload(ctx, repo, source_file, digest, media_type, chunked, chunk_size):
    """Upload a blob to a repository.

    By default uses the monolithic upload protocol (POST + PUT), which reads
    the file into memory.  With --chunked the streaming protocol is used
    (POST + N×PATCH + PUT), which is suitable for large blobs.

    Both modes verify the confirmed digest returned by the registry against
    DIGEST before reporting success.  Credentials are resolved automatically.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo_name, _ = parse_image_ref(repo)
    except ValueError as exc:
        _error(repo, str(exc))
        sys.exit(1)

    if repo.rstrip("/") != f"{registry}/{repo_name}":
        _error(repo, "Repository must be a plain 'registry/repository' without a tag or digest")
        sys.exit(1)
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        if chunked:
            with open(source_file, "rb") as fh:
                confirmed = upload_blob_chunked(
                    client=client,
                    repo=repo_name,
                    source=fh,
                    digest=digest,
                    content_type=media_type,
                    chunk_size=chunk_size,
                )
        else:
            with open(source_file, "rb") as fh:
                data = fh.read()
            confirmed = upload_blob(
                client=client,
                repo=repo_name,
                data=data,
                digest=digest,
                content_type=media_type,
            )
    except OSError as exc:
        _error(source_file, str(exc))
        sys.exit(1)
    except (AuthError, BlobError, requests.exceptions.RequestException) as exc:
        _error(f"{repo}@{digest}", str(exc))
        sys.exit(1)

    # Derive a canonical blob location from the confirmed digest.
    location = f"/v2/{repo_name}/blobs/{confirmed}"
    try:
        size = _file_size(source_file)
    except OSError:
        size = 0

    click.echo(
        json.dumps(
            {"digest": confirmed, "size": size, "location": location},
            indent=2,
        )
    )


# ===========================================================================
# blob mount
# ===========================================================================


@blob.command("mount")
@telemetry_options
@click.option(
    "--repo",
    "-r",
    required=True,
    metavar="REPO",
    help="Destination repository in 'registry/name' format.",
)
@click.option(
    "--digest",
    "-d",
    required=True,
    metavar="DIGEST",
    help="Blob digest to mount (e.g. sha256:abc...).",
)
@click.option(
    "--from-repo",
    required=True,
    metavar="SOURCE_REPO",
    help="Source repository name (without registry prefix) to mount from.",
)
@click.pass_context
@track_scenario("blob mount")
def blob_mount(ctx, repo, digest, from_repo):
    """Mount a blob from another repository without a data transfer.

    Requests a cross-repository blob mount from FROM_REPO into REPO.  A 201
    Created response indicates success.  A 202 Accepted response means the
    registry does not support mounting or cannot access the source; in that
    case the command exits 1 and directs you to use 'blob upload' instead.
    """
    insecure = ctx.obj.get("insecure", False) if ctx.obj else False

    try:
        registry, repo_name, _ = parse_image_ref(repo)
    except ValueError as exc:
        _error(repo, str(exc))
        sys.exit(1)

    if repo.rstrip("/") != f"{registry}/{repo_name}":
        _error(repo, "repository must be a plain 'registry/repo' without tag or digest")
        sys.exit(1)
    client = RegistryClient(TransportConfig(registry=registry, insecure=insecure))

    try:
        confirmed = mount_blob(
            client=client,
            repo=repo_name,
            digest=digest,
            from_repo=from_repo,
        )
    except (AuthError, BlobError, requests.exceptions.RequestException) as exc:
        _error(f"{repo}@{digest}", str(exc))
        sys.exit(1)

    location = f"/v2/{repo_name}/blobs/{confirmed}"
    click.echo(
        json.dumps(
            {"digest": confirmed, "status": "mounted", "location": location},
            indent=2,
        )
    )


# ===========================================================================
# Internal helpers
# ===========================================================================


def _error(reference: str, reason: str) -> None:
    """Print an error message to stderr, prefixed with the reference."""
    click.echo(f"Error [{reference}]: {reason}", err=True)


def _file_size(path: str) -> int:
    """Return the byte size of *path*."""
    import os
    return os.path.getsize(path)
