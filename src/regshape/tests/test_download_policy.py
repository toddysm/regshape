"""Policy bounds, compatibility selection and no-effect rejection for download clients."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from regshape.libs.artifacts import DownloadOptions as ArtifactOptions
from regshape.libs.artifacts import pull_artifact
from regshape.libs.blobs import DownloadOptions, get_blob
from regshape.libs.blobs.download import resolve_download_options
from regshape.libs.transport import RegistryClient, TransportConfig
from regshape.libs.transport.download import DownloadTransport
from regshape.libs.transport.middleware import MiddlewarePipeline
from regshape.tests.artifact_fixtures import Registry, address, document, edge


def _client(**kwargs: object) -> RegistryClient:
    """Construct a real base client with one synthetic resolved credential snapshot."""
    with patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)):
        return RegistryClient(TransportConfig("registry.example", **kwargs))


def test_defaults_helpers_and_immutability() -> None:
    """Public exports share the same frozen policy; sequential does not select legacy."""
    assert ArtifactOptions is DownloadOptions
    options = DownloadOptions()
    assert (options.concurrency, options.ranges_per_blob, options.range_size) == (4, 1, 8_388_608)
    assert options.max_retries is None and options.transport == "pooled"
    assert DownloadOptions.sequential() == DownloadOptions(concurrency=1)
    assert DownloadOptions.legacy().transport == "legacy"
    with pytest.raises(FrozenInstanceError):
        options.concurrency = 8


@pytest.mark.parametrize("field,value", [
    ("concurrency", True), ("concurrency", 0), ("concurrency", 33), ("concurrency", 1.5),
    ("ranges_per_blob", True), ("ranges_per_blob", 0), ("ranges_per_blob", 5),
    ("range_size", True), ("range_size", 65_535), ("range_size", 67_108_865),
    ("max_retries", False), ("max_retries", -1), ("max_retries", 11),
    ("max_retries", "3"), ("transport", "other"), ("scratch_dir", 12),
])
def test_invalid_options_before_effects(field: str, value: object) -> None:
    """Reject types/bounds before any pool construction or filesystem writes."""
    with (
        patch("regshape.libs.transport.download._DownloadAdapter", side_effect=AssertionError("pool allocated")),
        pytest.raises(ValueError),
    ):
        DownloadOptions(**{field: value})


@pytest.mark.parametrize("changes", [
    {}, {"concurrency": 2}, {"ranges_per_blob": 2}, {"max_retries": 0},
])
def test_invalid_legacy_relationships(changes: dict) -> None:
    """Legacy rejects modern defaults and retry overrides rather than ignoring them."""
    with pytest.raises(ValueError):
        DownloadOptions(transport="legacy", **changes)


def test_valid_bounds_and_scratch_revalidation(tmp_path: Path) -> None:
    """Accept exact inclusive bounds and reject a root removed after policy construction."""
    assert DownloadOptions(concurrency=32, ranges_per_blob=32, range_size=67_108_864, max_retries=10)
    assert DownloadOptions(concurrency=1, range_size=65_536, max_retries=0)
    root = tmp_path / "scratch"
    root.mkdir()
    options = DownloadOptions(scratch_dir=root)
    root.rmdir()
    with pytest.raises(ValueError, match="existing"):
        resolve_download_options(_client(), options)


def test_unsupported_custom_policy_and_new_consumers(caplog: pytest.LogCaptureFixture) -> None:
    """Only omitted-option existing operations retain custom clients with a notice."""
    custom = Mock()
    assert resolve_download_options(custom, None) == DownloadOptions.legacy()
    assert "original serial" in caplog.text
    with pytest.raises(ValueError, match="safe fork"):
        resolve_download_options(custom, DownloadOptions())
    with pytest.raises(ValueError, match="safe fork"):
        resolve_download_options(custom, None, requires_pooled=True)
    with pytest.raises(ValueError, match="batch/status"):
        resolve_download_options(custom, DownloadOptions.legacy(), requires_pooled=True)


def test_subclass_middleware_and_instance_customization_rejected() -> None:
    """Unacknowledged custom behavior never disappears into a base worker."""
    class Custom(RegistryClient):
        """Unsupported custom transport without an explicit safe-fork contract."""

    with patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)):
        subclass = Custom(TransportConfig("registry.example"))
    client = _client(middlewares=[Mock()])
    modified = _client()
    modified.get = Mock()
    replaced = _client()
    replaced._pipeline = MiddlewarePipeline()
    for original in (subclass, client, modified, replaced):
        assert not original.supports_download_fork()
        with (
            patch("regshape.libs.transport.download._DownloadAdapter", side_effect=AssertionError("pool allocated")),
            pytest.raises(ValueError),
        ):
            DownloadTransport(original, DownloadOptions())


def test_explicit_safe_fork_extension() -> None:
    """A custom override explicitly installs its own isolated behavior in the context."""
    class Custom(RegistryClient):
        """Example extension preserving custom state without sharing mutable worker data."""

        def fork_for_download(self, *, context: DownloadTransport) -> RegistryClient:
            """Delegate base isolation and add worker-local extension metadata."""
            worker = super().fork_for_download(context=context)
            worker.custom_state = []
            return worker

    with patch("regshape.libs.transport.client.resolve_credentials", return_value=(None, None)):
        client = Custom(TransportConfig("registry.example", middlewares=[Mock()]))
    with DownloadTransport(client, DownloadOptions()) as worker:
        assert worker is not client and worker.custom_state == []


def test_legacy_blob_bypasses_new_layer() -> None:
    """Even supported clients never initialize auth/pools under explicit legacy selection."""
    payload = b"legacy"
    registry = Registry(document(children=[]), {address(payload): payload})
    with patch("regshape.libs.blobs.operations.DownloadTransport", side_effect=AssertionError("pooled")):
        result = get_blob(registry, "repo", address(payload), download_options=DownloadOptions.legacy())
    assert result.digest == address(payload)
    assert len(registry.calls) == 1 and registry.responses[0].close_calls == 1


def test_explicit_unsupported_blob_and_artifact_before_io(tmp_path: Path) -> None:
    """Explicit unsupported policy fails before requests, partial files or staging."""
    registry = Registry(document(children=[]))
    with pytest.raises(ValueError):
        get_blob(registry, "repo", address(b"x"), str(tmp_path / "blob"), download_options=DownloadOptions())
    with pytest.raises(ValueError):
        pull_artifact(registry, "repo", "moving", tmp_path / "layout", download_options=DownloadOptions())
    assert registry.calls == [] and list(tmp_path.iterdir()) == []


def test_legacy_artifact_single_compatibility_notice(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Original artifact acquisition initializes no new layer and warns only once per invocation."""
    payload = b"config"
    registry = Registry(document(config=edge(payload)), {address(payload): payload})
    with patch("regshape.libs.artifacts.operations.DownloadTransport", side_effect=AssertionError("pooled")):
        pull_artifact(registry, "repo", "moving", tmp_path / "out")
    notices = [record for record in caplog.records if "original serial transport" in record.message]
    assert len(notices) == 1
