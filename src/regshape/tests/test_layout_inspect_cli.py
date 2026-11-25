"""Offline expanded inspect CLI contracts: golden tree, safe styling and exact failures.

Synthetic metadata uses the approved snapshot's names without claiming payload
hash verification. Native inspection tests skip unsupported safe-open platforms;
argument and unsupported-platform mapping tests remain portable.
"""

import inspect
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from regshape.cli import layout_inspect as display
from regshape.cli.main import regshape
from regshape.libs.artifacts.graph import (
    DOCKER_INDEX,
    DOCKER_MANIFEST,
    OCI_INDEX,
    OCI_MANIFEST,
)
from regshape.libs.artifacts.storage import require_readonly_filesystem
from regshape.libs.errors import ArtifactError
from regshape.libs.layout import (
    InspectionDiagnostic,
    LayoutInspectionError,
    LayoutInspectionResult,
    SourceLocation,
    inspect_layout,
)
from regshape.tests.artifact_fixtures import address, edge

SGR = re.compile(r"\x1b\[(?:0|36|39|90|1;31|1;33|1;34|1;35|1;36|1;39)m")


@pytest.fixture(autouse=True)
def supported_inspection(request: pytest.FixtureRequest) -> None:
    """Keep usage/platform guards portable while requiring safe native IO for layout fixtures."""
    if (
        "invalid_arguments" in request.node.name
        or "unsupported_platform" in request.node.name
    ):
        return
    if sys.platform not in ("linux", "darwin"):
        pytest.skip("Read-only inspection intentionally supports Linux/macOS only")
    try:
        require_readonly_filesystem()
    except ArtifactError:
        pytest.skip("Safe read-only primitives are unavailable")


def _runner() -> CliRunner:
    """Keep stdout/stderr separate on minimum Click 8.1 and current versions."""
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _raw(value: dict) -> bytes:
    """Encode synthetic metadata exactly like the approved tiny SDK example."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _layout(
    tmp_path: Path, manifests: list, objects: dict[str, bytes], **fields: object
) -> Path:
    """Write a local marker, entry index and explicitly listed objects without production IO helpers."""
    path = tmp_path / "layout"
    path.mkdir()
    (path / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    (path / "index.json").write_bytes(
        _raw(
            {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": manifests,
                **fields,
            }
        )
    )
    for digest, raw in objects.items():
        algorithm, encoded = digest.split(":")
        target = path / "blobs" / algorithm / encoded
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return path


def _tiny(tmp_path: Path, **fields: object) -> Path:
    """Create the pinned 420-byte artifact manifest with repeated config/content bytes."""
    payload = b"{}"
    manifest = _raw(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "artifactType": "application/vnd.example.payload",
            "config": edge(payload, "application/vnd.oci.empty.v1+json"),
            "layers": [edge(payload)],
        }
    )
    return _layout(
        tmp_path,
        [edge(manifest, OCI_MANIFEST)],
        {address(manifest): manifest, address(payload): payload},
        **fields,
    )


def _baseline(tmp_path: Path) -> Path:
    """Assemble source-owned snapshot metadata under its pinned names, with opaque stand-in bytes."""
    index = "sha256:c1bfb88b8e2488f7ce88ca7d8520dba7c2e840827df0b9454b41c3951ad9f6cb"
    manifests = [
        "sha256:3e909ef8b7486601ba8c396a5dbcd6354655619219b9bc2edfa4bc9058799c72",
        "sha256:c557ab563d495bc615161bd0632bd154411f80967512be3151315a1eeadb1651",
        "sha256:1199dc981377947d51590b7e79361ab64c17b6748a8e4a2a37475e4185e1a780",
        "sha256:3d0cfc6b813be4b277a2b0b46134fa134de5a3dbf427af91243e0ec230f408fc",
        "sha256:3f7ff85876b7f720a6a3ecaa317d48d97b0a2e1c07ff5858b689a577eb5e2aa0",
        "sha256:00175e7d3c6de0bd77f12b6e77d33ea815562a087253b3649a19dc6bf5bbf127",
    ]
    configs = [
        "sha256:b5e573137f3eb48cacc73be36b706aa05c853426fa8ee787c3fc66f9dd0c2131",
        "sha256:cb0cce4eda8f423407494ef007c9cb3f43fbdeb085239f37663f0b3a58a84845",
        "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    ]
    layers = [
        "sha256:58be4cd54090814ad90fff8481b9b4efa14ec48da55308ecb7dd8ba1ac1c15bf",
        "sha256:2863e8f27df388569919188f5c5f53d1c7ed444d95290c6ade1a33c295176ec3",
        "sha256:13bffcecfa3d5486c10d0377da452400bf7af439c1a299e49a6fc77eef66ddca",
        "sha256:ca95e05adb1550f58c9f35d515121aff44bb85cf5dfadd2b94600480f37b00ba",
        "sha256:77696d5344906864d08344a9bdf45c4efb2a4085b1d27e9fa7a7189f94f8c10a",
    ]
    objects = {digest: b"opaque" for digest in configs + layers}

    def descriptor(
        digest: str, media_type: str = "application/octet-stream", **extra: object
    ) -> dict:
        """Retain pinned identity while sizing each stand-in or metadata file truthfully."""
        return {
            "digest": digest,
            "mediaType": media_type,
            "size": len(objects[digest]),
            **extra,
        }

    for position in range(2):
        objects[manifests[position]] = _raw(
            {
                "schemaVersion": 2,
                "mediaType": DOCKER_MANIFEST,
                "config": descriptor(configs[position]),
                "layers": [
                    descriptor(digest)
                    for digest in layers[position * 2 : position * 2 + 2]
                ],
            }
        )
    objects[index] = _raw(
        {
            "schemaVersion": 2,
            "mediaType": DOCKER_INDEX,
            "manifests": [
                descriptor(
                    manifests[0],
                    DOCKER_MANIFEST,
                    platform={
                        "architecture": "amd64",
                        "os": "linux",
                        "os.version": "azurelinux-1.0",
                    },
                ),
                descriptor(
                    manifests[1],
                    DOCKER_MANIFEST,
                    platform={
                        "architecture": "arm64",
                        "os": "linux",
                        "os.version": "azurelinux-1.0",
                        "variant": "v8",
                    },
                ),
            ],
        }
    )
    thumbprints = (
        '["279fe22c1705ff7d5ab8a00f15b4321bc1120a891be5b01b6acde032fa1c8c5e", '
        '"9b1894f223d934cbd6575af3c6e1f6096b9221a7da132185f5a5cdc92235b5dc", '
        '"23ffe2b8bdb9a1711515d4cffda04bc7f793d513c76c243f1020507d8669b7db"]'
    )
    titles = [
        "/mnt/vss/_work/1/a/payloads/cHVibGljL2F6dXJlbGludXgvYnVzeWJveDoxLjM2LjEtYXpsMy4wLjIwMjYxMTIw.json",
        "/mnt/vss/_work/1/a/payloads/cHVibGljL2F6dXJlbGludXgvYnVzeWJveDoxLjM2.json",
    ]
    for position, created in enumerate(
        [
            "2026-11-22T18:40:19Z",
            "2026-11-22T18:40:18Z",
            "2026-11-22T18:12:55Z",
            "2026-11-22T18:12:51Z",
        ]
    ):
        signature = position < 2
        annotations = {}
        if signature:
            annotations["io.cncf.notary.x509chain.thumbprint#S256"] = thumbprints
        annotations["org.opencontainers.image.created"] = created
        if not signature:
            annotations["vnd.microsoft.artifact.lifecycle.end-of-life.date"] = (
                "2027-11-22T17:42:17Z"
            )
        objects[manifests[position + 2]] = _raw(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "artifactType": (
                    "application/vnd.cncf.notary.signature"
                    if signature
                    else "application/vnd.microsoft.artifact.lifecycle"
                ),
                "subject": descriptor(index, DOCKER_INDEX),
                "annotations": annotations,
                "config": descriptor(configs[2]),
                "layers": [
                    descriptor(
                        layers[4] if signature else configs[2],
                        **(
                            {
                                "annotations": {
                                    "org.opencontainers.image.title": titles[position]
                                }
                            }
                            if signature
                            else {}
                        ),
                    )
                ],
            }
        )
    return _layout(
        tmp_path,
        [
            descriptor(
                index,
                DOCKER_INDEX,
                annotations={"org.opencontainers.image.ref.name": "1.36"},
            ),
            *(descriptor(digest, OCI_MANIFEST) for digest in manifests[2:]),
        ],
        objects,
    )


class _Output(io.StringIO):
    """Capturable text stream with explicit terminal/encoding and injected delivery failures."""

    def __init__(
        self, encoding: str = "utf-8", terminal: bool = True, failure: str = ""
    ) -> None:
        """Keep failures opt-in so successful writes retain StringIO's complete content."""
        super().__init__()
        self._encoding = encoding
        self.terminal = terminal
        self.failure = failure

    @property
    def encoding(self) -> str:
        """Advertise the encoding used by renderer preflight rather than StringIO's None."""
        return self._encoding

    def isatty(self) -> bool:
        """Expose a deterministic terminal capability without requiring a real terminal."""
        return self.terminal

    def write(self, text: str) -> int:
        """Inject a broken pipe or short write without hiding already delivered bytes."""
        if self.failure == "write":
            raise BrokenPipeError("closed\n\x1b[31m")
        return super().write(text[:2] if self.failure == "short" else text)

    def flush(self) -> None:
        """Make deferred write failures observable before a command can claim success."""
        if self.failure == "flush":
            raise OSError("flush failed")
        super().flush()


def _streams(
    monkeypatch: pytest.MonkeyPatch, stdout: _Output, stderr: _Output | None = None
) -> None:
    """Replace only renderer stream selection; leave CliRunner's own capture lifecycle intact."""
    original = display._text_stream

    def stream(name: str) -> io.TextIOBase:
        """Borrow injected streams or the runner's actual stderr when not overridden."""
        if name == "stdout":
            return stdout
        return stderr if stderr is not None else original(name)

    monkeypatch.setattr(display, "_text_stream", stream)


def test_exact_normalized_approved_baseline(tmp_path: Path) -> None:
    """Reproduce every pinned heading/property/space and full value using only root normalization."""
    path = _baseline(tmp_path)
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    spec = (Path(__file__).parents[3] / "specs/cli/layout-inspect.md").read_text()
    expected = (
        spec.split("## Baseline real-image example", 1)[1]
        .split("```text\n", 1)[1]
        .split("```", 1)[0]
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.replace(str(path), "/examples/mcr-busybox-1.36", 1) == expected
    assert result.stderr == ""
    assert "\t" not in result.stdout and "\x1b" not in result.stdout


def test_json_is_sdk_serializer_not_a_cli_schema(tmp_path: Path) -> None:
    """Match the exact SDK JSON bytes, optional presence and repeated config/content occurrences."""
    path = _tiny(tmp_path, annotations={})
    sdk = inspect_layout(path)
    result = _runner().invoke(
        regshape, ["layout", "inspect", "--path", str(path), "--json"], color=True
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout == sdk.to_json()
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert value["kind"] == "layout"
    assert value["children"][0]["annotations"] == {}
    manifest = value["children"][0]["children"][0]
    assert manifest["children"][0]["digest"] == manifest["children"][1]["digest"]
    assert manifest["children"][1]["kind"] == "content"
    assert (
        "subject" not in manifest
        and "schema" not in value
        and "\x1b" not in result.stdout
    )


def test_source_positions_descriptor_ownership_and_valid_siblings(
    tmp_path: Path,
) -> None:
    """Invalid source slots do not renumber repeated targets or move incoming metadata onto nodes."""
    payload = b"{}"
    manifest = _raw(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "annotations": {"owner": "manifest"},
            "config": edge(payload),
            "layers": [
                edge(payload, annotations={"title": "payload-owner"}),
                edge(payload),
            ],
        }
    )
    path = _layout(
        tmp_path,
        [
            None,
            edge(
                manifest,
                OCI_MANIFEST,
                platform={"architecture": "arm64", "os": "linux"},
                annotations={"entry": "one"},
            ),
            edge(manifest, OCI_MANIFEST, annotations={"entry": "two"}),
        ],
        {address(manifest): manifest, address(payload): payload},
    )
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert result.exit_code == 1
    owner, first, second = result.stdout.split(f"Manifest {address(manifest)}")
    assert "[0] Error (INVALID_DESCRIPTOR):" in owner
    assert f"[1] Digest (digest): {address(manifest)}" in owner
    assert f"[2] Digest (digest): {address(manifest)}" in owner
    assert owner.index("entry: one") < owner.index("entry: two")
    assert '"architecture": "arm64"' in owner
    for occurrence in (first, second):
        assert "owner: manifest" in occurrence and "title: payload-owner" in occurrence
        assert "entry:" not in occurrence and "Platform (platform)" not in occurrence
        assert f"Layer 0 {address(payload)}" in occurrence
        assert f"Layer 1 {address(payload)}" in occurrence
        assert "title:" not in occurrence.split(f"Config {address(payload)}")[1]
    assert result.stderr.startswith("INVALID_DESCRIPTOR:")
    colored = display.render_inspection(
        inspect_layout(path),
        encoding="utf-8",
        ascii_only=False,
        color=True,
    )
    assert "\x1b[36m[0] \x1b[0m\x1b[1;31mError (INVALID_DESCRIPTOR)" in colored
    assert SGR.sub("", colored) == result.stdout


def test_color_spans_resets_and_plain_parity(tmp_path: Path) -> None:
    """Check exact label/value boundaries, all heading colors, connectors and complete line resets."""
    sdk = inspect_layout(_baseline(tmp_path))
    plain = display.render_inspection(
        sdk, encoding="utf-8", ascii_only=False, color=False
    )
    colored = display.render_inspection(
        sdk, encoding="utf-8", ascii_only=False, color=True
    )
    assert SGR.sub("", colored) == plain
    for name, code in [
        ("Index", "1;35"),
        ("Manifest", "1;36"),
        ("Config", "1;33"),
        ("Layer 0", "1;34"),
        ("Content 0", "1;34"),
    ]:
        assert f"\x1b[{code}m{name}\x1b[0m \x1b[39msha256:" in colored
    assert "\x1b[90m└── \x1b[0m" in colored
    assert "\x1b[36mLocation\x1b[0m\x1b[36m:\x1b[0m \x1b[39m" in colored
    assert (
        "\x1b[36m[0] Digest (digest)\x1b[0m\x1b[36m:\x1b[0m \x1b[39msha256:" in colored
    )
    assert all(line.endswith("\x1b[0m") for line in colored.splitlines())
    assert "\x1b[37m" not in colored


@pytest.mark.parametrize(
    "terminal,no_color,as_json,want_color",
    [
        (True, "", False, True),
        (True, "1", False, False),
        (True, "false", False, False),
        (False, "", False, False),
        (True, "", True, False),
    ],
)
def test_cli_color_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
    no_color: str,
    as_json: bool,
    want_color: bool,
) -> None:
    """Respect terminal detection/NO_COLOR independently of CliRunner's forced Click color."""
    path = _tiny(tmp_path)
    output = _Output(terminal=terminal)
    _streams(monkeypatch, output)
    monkeypatch.setenv("NO_COLOR", no_color)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(path)] + (["--json"] if as_json else []),
        color=True,
    )
    assert result.exit_code == 0, result.stderr
    assert ("\x1b[" in output.getvalue()) == want_color
    expected = (
        inspect_layout(path).to_json()
        if as_json
        else display.render_inspection(
            inspect_layout(path),
            encoding="utf-8",
            ascii_only=False,
            color=False,
        )
    )
    assert SGR.sub("", output.getvalue()) == expected


@pytest.mark.parametrize(
    "encoding,force_ascii,visible,escaped",
    [
        ("utf-8", False, "é雪🙂", None),
        ("utf-8", True, None, "\\u00e9\\u96ea\\U0001f642"),
        ("ascii", False, None, "\\u00e9\\u96ea\\U0001f642"),
        ("latin-1", False, "é", "\\u96ea\\U0001f642"),
    ],
)
def test_ascii_fallback_and_encoding_preserve_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    force_ascii: bool,
    visible: str | None,
    escaped: str | None,
) -> None:
    """Fallback connectors and value escaping never replace or truncate unencodable characters."""
    path = _tiny(tmp_path, annotations={"unicode": "é雪🙂"})
    output = _Output(encoding=encoding, terminal=False)
    _streams(monkeypatch, output)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(path)] + (["--ascii"] if force_ascii else []),
    )
    assert result.exit_code == 0, result.stderr
    text = output.getvalue()
    if visible:
        assert visible in text
    if escaped:
        assert escaped in text
    if force_ascii or encoding != "utf-8":
        assert "`-- Index" in text and "|-- Config" in text
        assert "└" not in text and "│" not in text
    assert "?" not in text
    text.encode(encoding)


def test_real_ascii_process_streams_are_not_upgraded_by_click(tmp_path: Path) -> None:
    """Keep actual ASCII stdout rendering and stderr diagnostics ASCII, without fake streams."""
    path = _tiny(tmp_path, annotations={"unicode": "é雪🙂"})
    code = """
from regshape.cli.main import regshape
from regshape.cli.layout_inspect import emit_inspection_diagnostics
from regshape.libs.layout import InspectionDiagnostic, SourceLocation
regshape(standalone_mode=False)
emit_inspection_diagnostics((InspectionDiagnostic("TEST", "\\u00e9\\u96ea\\U0001f642", SourceLocation("index.json")),))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, "layout", "inspect", "-p", str(path)],
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[2]),
            "PYTHONIOENCODING": "ascii",
        },
    )
    assert completed.returncode == 0, completed.stderr
    text = completed.stdout.decode("ascii")
    assert "`-- Index" in text and "|-- Config" in text
    assert "\\u00e9\\u96ea\\U0001f642" in text
    assert completed.stderr.decode("ascii") == "TEST: \\u00e9\\u96ea\\U0001f642\n"


def test_terminal_controls_are_escaped_in_every_untrusted_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escape paths, annotation keys/values, platform JSON, messages and sources before styling."""
    hostile = "\\\n\t\r\x00\x1b[31m\x7f\x85\u202e\u200d"
    path = _tiny(tmp_path, annotations={f"key{hostile}": hostile})
    renamed = path.with_name("layout" + hostile.replace("\x00", "").replace("\\", ""))
    path.rename(renamed)
    sdk = inspect_layout(renamed)
    diagnostic = InspectionDiagnostic(
        "INVALID_DESCRIPTOR",
        hostile,
        SourceLocation("source" + hostile, "/pointer" + hostile),
        location="location" + hostile,
    )
    root = sdk.root
    index = root.children[0]
    descriptor = replace(
        index.manifests[0],
        platform={"architecture": "arm64", "os": "linux", "extra": hostile},
    )
    sdk = replace(
        sdk,
        root=replace(
            root,
            errors=(diagnostic,),
            children=(replace(index, manifests=(descriptor,)),),
        ),
        diagnostics=(diagnostic,),
    )
    monkeypatch.setattr("regshape.cli.layout.inspect_layout", lambda path: sdk)
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", "unused"])
    assert result.exit_code == 1
    for text in (result.stdout, result.stderr):
        assert all(char == "\n" or ord(char) >= 32 for char in text)
        for unsafe in ("\r", "\t", "\x1b", "\x7f", "\x85", "\u202e", "\u200d"):
            assert unsafe not in text
        assert "\\u001b[31m" in text
        assert "\\u202e" in text and "\\u200d" in text
        assert "\\\\\\n\\t\\r\\u0000" in text
    assert "Error (INVALID_DESCRIPTOR)" in result.stdout
    machine = _runner().invoke(
        regshape, ["layout", "inspect", "-p", "unused", "--json"]
    )
    assert machine.exit_code == 1
    assert machine.stdout == sdk.to_json()
    assert json.loads(machine.stdout)["errors"][0]["message"] == hostile


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_shared_payload_exact_streams_and_status(
    tmp_path: Path,
    as_json: bool,
) -> None:
    """Retain both missing occurrences and SDK diagnostics, with red labels only in colored text."""
    path = _tiny(tmp_path)
    digest = address(b"{}")
    location = "blobs/" + digest.replace(":", "/")
    (path / location).unlink()
    sdk = inspect_layout(path)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(path)] + (["--json"] if as_json else []),
    )
    assert result.exit_code == 1
    assert result.stderr == "".join(
        f"{error.code}: {error.message}; Location: {error.location}; Source: {error.source}\n"
        for error in sdk.diagnostics
    )
    assert len(sdk.diagnostics) == 2
    if as_json:
        assert result.stdout == sdk.to_json()
    else:
        assert result.stdout.count(f"Location: {location}") == 2
        assert (
            result.stdout.count(
                "Missing file (MISSING_OBJECT): Referenced file is missing"
            )
            == 2
        )
        assert "absent" not in result.stdout and "present" not in result.stdout
        colored = display.render_inspection(
            sdk, encoding="utf-8", ascii_only=False, color=True
        )
        assert "\x1b[1;31mMissing file (MISSING_OBJECT)\x1b[0m" in colored
        assert SGR.sub("", colored) == result.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--path", "unused", "--ascii", "--json"],
        ["--path", "unused", "--summary"],
        ["--path", ""],
        ["--path", "\x00"],
    ],
)
def test_invalid_arguments_never_inspect(args: list[str]) -> None:
    """Missing required options and incompatible/unimplemented flags remain usage errors before IO."""
    with patch("regshape.cli.layout.inspect_layout") as inspector:
        result = _runner().invoke(regshape, ["layout", "inspect", *args])
    assert result.exit_code == 2
    assert result.stdout == "" and result.stderr
    inspector.assert_not_called()


def test_unsupported_platform_mapping_without_tree() -> None:
    """Map the SDK platform exception to plain stderr/exit 1 without a traceback or empty success."""
    diagnostic = InspectionDiagnostic(
        "UNSUPPORTED_PLATFORM",
        "Read-only safe opens unavailable",
        SourceLocation("layout"),
    )
    with patch(
        "regshape.cli.layout.inspect_layout",
        side_effect=LayoutInspectionError((diagnostic,)),
    ):
        result = _runner().invoke(
            regshape, ["layout", "inspect", "-p", "unused", "--json"]
        )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "UNSUPPORTED_PLATFORM: Read-only safe opens unavailable\n"


@pytest.mark.parametrize("as_json", [False, True])
def test_invalid_root_empty_stdout_and_exact_stderr(
    tmp_path: Path, as_json: bool
) -> None:
    """A missing marker is an SDK root failure, not Click existence validation or a partial tree."""
    path = tmp_path / "invalid-layout"
    path.mkdir()
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(path)] + (["--json"] if as_json else []),
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert (
        result.stderr
        == "INVALID_LAYOUT: Required layout marker oci-layout is missing\n"
    )


def test_renderer_never_serializes_or_reopens_sdk_result(tmp_path: Path) -> None:
    """Text consumes typed occurrences directly, without CLI discovery, file reads or JSON copies."""
    sdk = inspect_layout(_tiny(tmp_path))
    with (
        patch("builtins.open", side_effect=AssertionError("no CLI file reads")),
        patch("os.open", side_effect=AssertionError("no CLI safe opens")),
        patch("os.stat", side_effect=AssertionError("no CLI metadata reads")),
        patch.object(
            LayoutInspectionResult,
            "to_dict",
            side_effect=AssertionError("no expanded copy"),
        ),
        patch.object(
            LayoutInspectionResult,
            "to_json",
            side_effect=AssertionError("no JSON proxy"),
        ),
    ):
        text = display.render_inspection(
            sdk, encoding="utf-8", ascii_only=False, color=False
        )
    assert "OCI layout\n" in text and "Content 0 sha256:" in text


def test_cli_delegates_path_and_metadata_without_its_own_io(tmp_path: Path) -> None:
    """Delegate all layout reads/stats; unrelated Click localization may probe its own catalog files."""
    sdk = inspect_layout(_tiny(tmp_path))
    with (
        patch("regshape.cli.layout.inspect_layout", return_value=sdk) as inspector,
        patch("builtins.open", side_effect=AssertionError("no CLI file reads")),
        patch("os.open", side_effect=AssertionError("no CLI safe opens")),
        patch("os.stat", wraps=os.stat) as stat,
    ):
        result = _runner().invoke(
            regshape, ["layout", "inspect", "-p", "sdk-owned-path"]
        )
    assert result.exit_code == 0, result.stderr
    inspector.assert_called_once_with("sdk-owned-path")
    assert all(
        "sdk-owned-path" not in str(call.args[0])
        and sdk.root.location not in str(call.args[0])
        for call in stat.call_args_list
    )
    assert "OCI layout\n" in result.stdout


@pytest.mark.parametrize(
    "damage,code",
    [
        ("malformed", "MALFORMED_DOCUMENT"),
        ("unsupported", "UNSUPPORTED_TYPE"),
        ("unsafe", "UNREADABLE_OBJECT"),
        ("conflict", "CONFLICTING_DESCRIPTOR"),
    ],
)
def test_partial_document_failures_keep_identity_and_safe_siblings(
    tmp_path: Path,
    damage: str,
    code: str,
) -> None:
    """Distinguish unknown, malformed, unsafe and conflicting occurrences without dropping a sibling."""
    valid = _raw({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": []})
    sibling = _raw(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [],
            "annotations": {"sibling": "retained"},
        }
    )
    broken = b"{bad" if damage == "malformed" else valid
    descriptor = edge(
        broken,
        "application/vnd.example.unsupported" if damage == "unsupported" else OCI_INDEX,
    )
    if damage == "conflict":
        descriptor["size"] += 1
    path = _layout(
        tmp_path,
        [descriptor, edge(sibling, OCI_INDEX)],
        {address(broken): broken, address(sibling): sibling},
    )
    if damage == "unsafe":
        target = path / "blobs/sha256" / address(broken).split(":")[1]
        target.unlink()
        target.symlink_to(path / "index.json")
    sdk = inspect_layout(path)
    assert [error.code for error in sdk.diagnostics] == [code]
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"{code}: ")
    assert f"Error ({code}):" in result.stdout
    assert f"Location: blobs/sha256/{address(broken).split(':')[1]}" in result.stdout
    assert f"Index {address(sibling)}" in result.stdout
    if damage == "unsupported":
        assert f"Unknown {address(broken)}" in result.stdout
    machine = _runner().invoke(
        regshape, ["layout", "inspect", "-p", str(path), "--json"]
    )
    assert machine.exit_code == 1
    assert machine.stdout == sdk.to_json() and machine.stderr == result.stderr


def test_repeated_indexes_cycles_and_traversal_limits_are_not_elided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expand sibling index reuse, terminate only cycles, and retain SDK limit markers/partial status."""
    digest = "sha256:" + "a" * 64
    descriptor = {"digest": digest, "mediaType": OCI_INDEX, "size": 1}
    nested = _raw(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [descriptor]}
    )
    descriptor["size"] = len(nested)
    nested = _raw(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [descriptor]}
    )
    descriptor["size"] = len(nested)
    nested = _raw(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": [descriptor]}
    )
    path = _layout(tmp_path, [descriptor, descriptor], {digest: nested})
    sdk = inspect_layout(path)
    assert [error.code for error in sdk.diagnostics] == ["CYCLE", "CYCLE"]
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert result.exit_code == 1
    assert result.stdout.count(f"Index {digest}\n") == 4
    assert result.stdout.count("Error (CYCLE):") == 2
    assert "already shown" not in result.stdout and "shared" not in result.stdout
    limited = inspect_layout(path, limits=replace(sdk.limits, max_depth=1))
    monkeypatch.setattr("regshape.cli.layout.inspect_layout", lambda path: limited)
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert result.exit_code == 1
    assert "Error (LIMIT_REACHED):" in result.stdout and "max_depth" in result.stderr
    assert f"Index {digest}\n" not in result.stdout


@pytest.mark.parametrize(
    "encoding,color",
    [("utf-8", False), ("utf-8", True), ("utf-16", False), ("utf-7", False)],
)
def test_text_budget_exact_inclusive_encoded_boundary(
    tmp_path: Path,
    encoding: str,
    color: bool,
) -> None:
    """Count all encoded indentation, LF, escapes and ANSI, including a single stateful BOM."""
    sdk = inspect_layout(
        _tiny(
            tmp_path,
            annotations={"control": "\x00" * 100, "unicode": "é" * 1024},
        )
    )
    text = display.render_inspection(
        sdk, encoding=encoding, ascii_only=False, color=color
    )
    size = len(text.encode(encoding))
    exact = replace(sdk, limits=replace(sdk.limits, max_output_bytes=size))
    assert (
        display.render_inspection(
            exact, encoding=encoding, ascii_only=False, color=color
        )
        == text
    )
    over = replace(sdk, limits=replace(sdk.limits, max_output_bytes=size - 1))
    with pytest.raises(LayoutInspectionError) as error:
        display.render_inspection(
            over, encoding=encoding, ascii_only=False, color=color
        )
    assert error.value.diagnostics[0].code == "OUTPUT_LIMIT"


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_output_cap_refuses_all_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    """Fail one byte over the selected representation cap without writing cut-off output."""
    sdk = inspect_layout(_tiny(tmp_path))
    content = (
        sdk.to_json()
        if as_json
        else display.render_inspection(
            sdk,
            encoding="utf-8",
            ascii_only=False,
            color=False,
        )
    )
    cap = len(content.encode()) - 1
    limited = replace(sdk, limits=replace(sdk.limits, max_output_bytes=cap))
    monkeypatch.setattr("regshape.cli.layout.inspect_layout", lambda path: limited)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", "unused"] + (["--json"] if as_json else []),
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"OUTPUT_LIMIT: max_output_bytes cap {cap} exceeded\n"


def test_preflight_stops_before_materializing_amplified_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tiny output cap stops the first counting pass before escaping a whole large annotation."""
    sdk = inspect_layout(_tiny(tmp_path, annotations={"large": "\x00" * 100_000}))
    limited = replace(sdk, limits=replace(sdk.limits, max_output_bytes=4096))
    chunks = display._Tree.chunks
    visits = []

    def counted(self: display._Tree, root: object):
        """Record bounded emitted chunks and passes, not a fully materialized representation."""
        visits.append("pass")
        for chunk in chunks(self, root):
            visits.append(len(chunk))
            yield chunk

    monkeypatch.setattr(display._Tree, "chunks", counted)
    with pytest.raises(LayoutInspectionError):
        display.render_inspection(
            limited, encoding="utf-8", ascii_only=False, color=False
        )
    assert visits.count("pass") == 1
    assert sum(value for value in visits if isinstance(value, int)) < 8192


@pytest.mark.parametrize("failure", ["write", "flush", "short"])
@pytest.mark.parametrize("as_json", [False, True])
def test_delivery_failure_is_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    as_json: bool,
) -> None:
    """Broken pipes, deferred failures and short writes return 1 with escaped diagnostic stderr."""
    path = _tiny(tmp_path)
    output = _Output(terminal=False, failure=failure)
    _streams(monkeypatch, output)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(path)] + (["--json"] if as_json else []),
    )
    assert result.exit_code == 1
    assert result.stderr.startswith("OUTPUT_WRITE_FAILED:")
    assert not output.closed
    assert "\x1b" not in result.stderr
    if failure == "write":
        assert output.getvalue() == ""
        assert "\\n\\u001b[31m" in result.stderr


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("encoding", ["punycode", "idna"])
def test_non_stream_stdout_codec_fails_explicitly_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
    encoding: str,
) -> None:
    """Report unsupported destination encoding as a delivery failure, with no stdout or traceback."""
    output = _Output(encoding=encoding, terminal=False)
    _streams(monkeypatch, output)
    result = _runner().invoke(
        regshape,
        ["layout", "inspect", "-p", str(_tiny(tmp_path))]
        + (["--json"] if as_json else []),
    )
    assert result.exit_code == 1
    assert output.getvalue() == ""
    assert result.stderr.startswith("OUTPUT_WRITE_FAILED:")
    assert "stream character encoding" in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.closed


def test_unwritable_stderr_still_returns_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both streams fail there is no success-shaped fallback or leaked raw traceback."""
    output = _Output(failure="write")
    stderr = _Output(failure="write")
    _streams(monkeypatch, output, stderr)
    result = _runner().invoke(
        regshape, ["layout", "inspect", "-p", str(_tiny(tmp_path))]
    )
    assert result.exit_code == 1
    assert result.stdout == result.stderr == ""
    assert not output.closed and not stderr.closed


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("state", ["stdout-absent", "both-absent", "stdout-closed"])
def test_missing_or_closed_process_streams_fail_without_traceback(
    tmp_path: Path,
    as_json: bool,
    state: str,
) -> None:
    """Keep delivery failures explicit even when Python starts without standard descriptors.

    The driver classifies unhandled stream errors as exit 97, not the expected
    controlled exit 1, even when both descriptors prevent traceback capture.
    """
    bootstrap = """
import os
import sys
if os.environ["REGSHAPE_TEST_STREAM_STATE"] != "stdout-closed":
    os.close(1)
if os.environ["REGSHAPE_TEST_STREAM_STATE"] == "both-absent":
    os.close(2)
os.execv(sys.executable, [sys.executable, "-c", sys.argv[1], *sys.argv[2:]])
"""
    driver = """
import os
import sys
from regshape.cli.main import regshape
if os.environ["REGSHAPE_TEST_STREAM_STATE"] == "stdout-closed":
    sys.stdout.close()
try:
    regshape()
except (AttributeError, OSError, ValueError):
    raise SystemExit(97)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            driver,
            "layout",
            "inspect",
            "-p",
            str(_tiny(tmp_path)),
            *(["--json"] if as_json else []),
        ],
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[2]),
            "REGSHAPE_TEST_STREAM_STATE": state,
        },
    )
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout == b""
    if state == "both-absent":
        assert completed.stderr == b""
    else:
        assert completed.stderr.startswith(b"OUTPUT_WRITE_FAILED:")
        assert completed.stderr.count(b"\n") == 1
    assert b"Traceback" not in completed.stderr


@pytest.mark.parametrize("as_json", [False, True])
def test_real_closed_pipe_exits_one_without_shutdown_diagnostic(
    tmp_path: Path,
    as_json: bool,
) -> None:
    """A closed real pipe must not retry buffered output at interpreter shutdown and exit 120."""
    path = _tiny(tmp_path)
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "wb") as stdout:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from regshape.cli.main import regshape; regshape()",
                "layout",
                "inspect",
                "-p",
                str(path),
                *(["--json"] if as_json else []),
            ],
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2])},
        )
    assert completed.returncode == 1, completed.stderr
    assert completed.stderr.startswith("OUTPUT_WRITE_FAILED:")
    assert completed.stderr.count("\n") == 1
    assert "Exception ignored" not in completed.stderr
    assert "BrokenPipeError" not in completed.stderr


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("stderr_fails", [False, True])
def test_buffered_enospc_exits_one_without_shutdown_retry(
    tmp_path: Path,
    as_json: bool,
    stderr_fails: bool,
) -> None:
    """Keep exit 1 for full buffered stdout, even when diagnostic stderr also has EPIPE."""
    code = '''
import errno
import io
import os
import sys
class Failing(io.RawIOBase):
    """Synthetic failed device; no external file or parent descriptor is modified."""
    def __init__(self, number):
        """Select a stream-specific failure while retaining normal RawIOBase lifetime."""
        super().__init__()
        self.number = number
    def writable(self):
        """Allow BufferedWriter to accept pending output."""
        return True
    def write(self, data):
        """Fail the actual buffered flush with the selected operating-system error."""
        raise OSError(self.number, "synthetic output device failure")
sys.stdout = io.TextIOWrapper(io.BufferedWriter(Failing(errno.ENOSPC)), encoding="utf-8")
if os.environ["REGSHAPE_TEST_STDERR_FAILURE"] == "1":
    sys.stderr = io.TextIOWrapper(io.BufferedWriter(Failing(errno.EPIPE)), encoding="utf-8")
from regshape.cli.main import regshape
regshape()
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "layout",
            "inspect",
            "-p",
            str(_tiny(tmp_path)),
            *(["--json"] if as_json else []),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[2]),
            "REGSHAPE_TEST_STDERR_FAILURE": str(int(stderr_fails)),
        },
    )
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout == ""
    if stderr_fails:
        assert completed.stderr == ""
    else:
        assert completed.stderr.startswith("OUTPUT_WRITE_FAILED:")
        assert completed.stderr.count("\n") == 1
    assert "Exception ignored" not in completed.stderr


def test_encoded_json_cap_precedes_sdk_string_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject UTF-16 amplification in the SDK counting pass, not after allocating ASCII JSON."""
    sdk = inspect_layout(_tiny(tmp_path))
    content = sdk.to_json()
    cap = len(content.encode("utf-16")) - 1
    assert len(content.encode("ascii")) < cap
    limited = replace(sdk, limits=replace(sdk.limits, max_output_bytes=cap))
    monkeypatch.setattr("regshape.cli.layout.inspect_layout", lambda path: limited)
    output = _Output(encoding="utf-16", terminal=False)
    _streams(monkeypatch, output)
    original = LayoutInspectionResult.to_json
    materialized = []

    def serialize(result: LayoutInspectionResult, **kwargs: str) -> str:
        """Record only completed string allocations while forwarding new SDK counting options."""
        value = original(result, **kwargs)
        materialized.append(len(value))
        return value

    monkeypatch.setattr(LayoutInspectionResult, "to_json", serialize)
    result = _runner().invoke(regshape, ["layout", "inspect", "-p", "unused", "--json"])
    assert result.exit_code == 1
    assert result.stderr == f"OUTPUT_LIMIT: max_output_bytes cap {cap} exceeded\n"
    assert output.getvalue() == ""
    assert materialized == []


def test_present_empty_fields_and_legacy_show_are_unchanged(tmp_path: Path) -> None:
    """A valid empty index retains its selected empty fields; legacy show emits no migration warning."""
    path = _layout(tmp_path, [], {}, annotations={})
    inspected = _runner().invoke(regshape, ["layout", "inspect", "-p", str(path)])
    assert inspected.exit_code == 0, inspected.stderr
    assert "Referenced objects (manifests): []\n" in inspected.stdout
    assert "Annotations (annotations): {}\n" in inspected.stdout
    assert "Artifact type" not in inspected.stdout and "Subject" not in inspected.stdout
    shown = _runner().invoke(regshape, ["layout", "show", "-p", str(path)])
    assert shown.exit_code == 0
    assert shown.stderr == ""
    assert json.loads(shown.stdout) == json.loads((path / "index.json").read_bytes())
