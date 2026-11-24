"""Bounded read-only layout inspection, independent of verification and the CLI.

Only the marker, entry point and supported contained index/manifest metadata
are opened. Target stat/read failures are cached by address; subjects remain
source facts. Immutable fields are shared while every containment occurrence
retains its own descriptor provenance, diagnostics and referrers-list evidence.
"""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from itertools import chain
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypedDict, cast

from regshape.libs.artifacts.graph import (
    DOCUMENT_TYPES,
    INDEX_TYPES,
    MANIFEST_TYPES,
    OCI_INDEX,
    REF_NAME,
    strict_json,
)
from regshape.libs.artifacts.models import ArtifactLimits, digest_parts
from regshape.libs.artifacts.storage import SafeDirectory, require_readonly_filesystem
from regshape.libs.errors import ArtifactError
from regshape.libs.layout.inspection_models import (
    DescriptorView,
    DiagnosticCode,
    IndexNode,
    InspectionDiagnostic,
    InspectionLimits,
    JsonValue,
    LayoutInspectionError,
    LayoutInspectionResult,
    LayoutNode,
    ManifestNode,
    ObjectNode,
    OpaqueNode,
    ReferrerListEvidence,
    SourceLocation,
    UnknownNode,
    _StorageFacts,
)

_TAG = re.compile(r"(sha256|sha512)-([0-9a-f]{64})")


@dataclass(frozen=True)
class _Problem:
    """Cached source failure; activation attaches it to the current occurrence."""

    code: DiagnosticCode
    message: str
    source: SourceLocation


class _InputFailure(Exception):
    """Expected local read/parse failure, carrying a typed structural problem."""

    def __init__(self, problem: _Problem) -> None:
        """Transfer the specific cause to the root or descendant boundary."""
        self.problem = problem
        super().__init__(problem.message)


@dataclass(frozen=True)
class _Document:
    """Shared parsed fields and descriptor slots, without containment expansion."""

    kind: str
    media_type: str | None
    artifact_type: str | None
    annotations: Mapping[str, JsonValue] | None
    subject: DescriptorView | None
    manifests: tuple[DescriptorView, ...] | None
    config: DescriptorView | None
    layers: tuple[DescriptorView, ...] | None


@dataclass
class _Observation:
    """One attempted physical address; a later document role may parse it once."""

    size: int | None = None
    identity: os.stat_result | None = None
    problem: _Problem | None = None
    document: _Document | None = None
    document_attempted: bool = False
    document_problem: _Problem | None = None


class _Identity(TypedDict):
    """Constructor identity fields shared by the discriminated occurrence types."""

    location: str
    digest: str
    source: SourceLocation
    incoming: DescriptorView
    source_position: int | None


@dataclass
class _Pending:
    """Mutable bounded traversal frame, frozen bottom-up before returning."""

    node: ObjectNode
    depth: int
    ancestors: frozenset[str]
    slots: tuple[tuple[DescriptorView, str, int | None], ...] = ()
    children: list[_Pending] = field(default_factory=list)
    next_slot: int = 0


def _freeze(value: object) -> JsonValue:
    """Freeze bounded parsed JSON, preserving map order and explicit null extensions."""
    completed: dict[int, JsonValue] = {}
    stack: list[tuple[object, bool]] = [(value, False)]
    while stack:
        current, visited = stack.pop()
        if isinstance(current, (dict, list)) and not visited:
            stack.append((current, True))
            children = current.values() if isinstance(current, dict) else current
            stack.extend((item, False) for item in children)
        elif isinstance(current, dict):
            completed[id(current)] = MappingProxyType(
                {key: completed[id(item)] for key, item in current.items()}
            )
        elif isinstance(current, list):
            completed[id(current)] = tuple(completed[id(item)] for item in current)
        else:
            completed[id(current)] = cast(JsonValue, current)
    return completed[id(value)]


def _json(
    raw: bytes, limits: InspectionLimits, source: SourceLocation
) -> dict[str, object]:
    """Reuse strict parsing and additionally reject surrogates/overflowed numbers.

    Ignored extensions are still checked for valid scalar values and nesting.
    :raises _InputFailure: For malformed JSON or the explicit JSON depth cap.
    """
    try:
        document = strict_json(
            raw, ArtifactLimits(max_json_depth=limits.max_json_depth), len(raw)
        )
    except ArtifactError as error:
        depth = "max_json_depth" in str(error)
        raise _InputFailure(
            _Problem(
                "LIMIT_REACHED" if depth else "MALFORMED_DOCUMENT",
                f"max_json_depth cap {limits.max_json_depth} exceeded"
                if depth
                else str(error),
                source,
            )
        ) from error
    stack: list[object] = [document]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and any(
            0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise _InputFailure(
                _Problem(
                    "MALFORMED_DOCUMENT", "Surrogate code points are invalid", source
                )
            )
        elif isinstance(value, float) and not math.isfinite(value):
            raise _InputFailure(
                _Problem("MALFORMED_DOCUMENT", "Non-finite JSON number", source)
            )
    return document


def _annotations(value: object) -> bool:
    """Whether an annotation value is a string-pair map, including the empty map."""
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _platform(value: object) -> bool:
    """Validate known platform fields while retaining arbitrary valid JSON extensions."""
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) for key in ("os", "architecture")
    ):
        return False
    for key in ("os.version", "variant"):
        if key in value and not isinstance(value[key], str):
            return False
    for key in ("os.features", "features"):
        if key in value and (
            not isinstance(value[key], list)
            or not all(isinstance(item, str) for item in value[key])
        ):
            return False
    return True


def _media_type(value: object) -> bool:
    """Accept nonempty media-type strings without whitespace/control characters."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(ord(char) > 32 and ord(char) != 127 for char in value)
    )


def _valid_digest(value: object) -> str | None:
    """Return a safe digest or None for a field requiring an owner-local diagnostic."""
    if not isinstance(value, str):
        return None
    try:
        digest_parts(value)
    except ValueError:
        return None
    return value


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Comparable metadata identity/size/timestamps, not a content-integrity hash."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _descriptor(value: object, source: SourceLocation) -> DescriptorView:
    """Retain independently valid display fields and errors at the original slot.

    Invalid integrity fields do not authorize unsafe paths. URLs/data are checked
    for declared types only, never fetched, decoded, hashed or emitted.
    """
    if not isinstance(value, dict):
        return DescriptorView(
            source,
            errors=(
                InspectionDiagnostic(
                    "INVALID_DESCRIPTOR",
                    "Descriptor must be an object",
                    source,
                ),
            ),
        )
    errors: list[InspectionDiagnostic] = []
    digest = _valid_digest(value.get("digest"))
    if digest is None:
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR", "Invalid descriptor digest", source
            )
        )
    size = value.get("size")
    if type(size) is not int or size < 0:
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR",
                "Descriptor size must be a nonnegative integer",
                source,
            )
        )
        size = None
    media_type = value.get("mediaType")
    if not _media_type(media_type):
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR",
                "Descriptor mediaType must be a nonempty media type",
                source,
            )
        )
        media_type = None
    annotation_map = value.get("annotations")
    if "annotations" in value and not _annotations(annotation_map):
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR",
                "Descriptor annotations must be string pairs",
                source,
            )
        )
        annotation_map = None
    platform = value.get("platform")
    if "platform" in value and not _platform(platform):
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR", "Invalid descriptor platform", source
            )
        )
        platform = None
    artifact_type = value.get("artifactType")
    if "artifactType" in value and not _media_type(artifact_type):
        errors.append(
            InspectionDiagnostic(
                "INVALID_DESCRIPTOR", "Invalid descriptor artifactType", source
            )
        )
        artifact_type = None
    for key in ("data", "urls"):
        if key in value and (
            (key == "data" and not isinstance(value[key], str))
            or (
                key == "urls"
                and (
                    not isinstance(value[key], list)
                    or not all(isinstance(item, str) for item in value[key])
                )
            )
        ):
            errors.append(
                InspectionDiagnostic(
                    "INVALID_DESCRIPTOR", f"Invalid descriptor {key}", source
                )
            )
    return DescriptorView(
        source=source,
        digest=cast(str | None, digest),
        size=cast(int | None, size),
        media_type=cast(str | None, media_type),
        artifact_type=cast(str | None, artifact_type),
        annotations=cast(Mapping[str, JsonValue] | None, _freeze(annotation_map)),
        platform=cast(Mapping[str, JsonValue] | None, _freeze(platform)),
        errors=tuple(errors),
    )


def _document(value: dict[str, object], source: SourceLocation) -> _Document:
    """Validate intrinsic document shape without dropping invalid descriptor slots."""

    def fail(message: str, code: DiagnosticCode = "MALFORMED_DOCUMENT") -> None:
        """Stop intrinsic parsing rather than manufacturing trusted empty fields."""
        raise _InputFailure(_Problem(code, message, source))

    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 2:
        fail("schemaVersion must be integer 2")
    media_type = value.get("mediaType")
    if "mediaType" in value and not _media_type(media_type):
        fail("Invalid document mediaType")
    if media_type is not None and media_type not in DOCUMENT_TYPES:
        fail("Unsupported required document media type", "UNSUPPORTED_TYPE")
    artifact_type = value.get("artifactType")
    if "artifactType" in value and not _media_type(artifact_type):
        fail("Invalid document artifactType")
    annotations = value.get("annotations")
    if "annotations" in value and not _annotations(annotations):
        fail("Document annotations must be string pairs")
    is_index = (
        media_type in INDEX_TYPES if media_type is not None else "manifests" in value
    )
    if is_index:
        if (
            not isinstance(value.get("manifests"), list)
            or "config" in value
            or "layers" in value
        ):
            fail("Index requires manifests without config/layers")
    elif (
        not isinstance(value.get("layers"), list)
        or "config" not in value
        or "manifests" in value
    ):
        fail("Manifest requires config/layers without manifests")
    document = source.document
    subject = (
        _descriptor(value["subject"], SourceLocation(document, "/subject"))
        if "subject" in value
        else None
    )
    manifests = (
        tuple(
            _descriptor(item, SourceLocation(document, f"/manifests/{position}"))
            for position, item in enumerate(cast(list[object], value["manifests"]))
        )
        if is_index
        else None
    )
    layers = (
        tuple(
            _descriptor(item, SourceLocation(document, f"/layers/{position}"))
            for position, item in enumerate(cast(list[object], value["layers"]))
        )
        if not is_index
        else None
    )
    config = (
        _descriptor(value["config"], SourceLocation(document, "/config"))
        if not is_index
        else None
    )
    return _Document(
        "index" if is_index else "manifest",
        cast(str | None, media_type),
        cast(str | None, artifact_type),
        cast(Mapping[str, JsonValue] | None, _freeze(annotations)),
        subject,
        manifests,
        config,
        layers,
    )


def _root_diagnostics(
    diagnostics: tuple[InspectionDiagnostic, ...], limits: InspectionLimits
) -> tuple[InspectionDiagnostic, ...]:
    """Bound nonempty no-tree failures, reserving the final slot like traversal errors."""
    cap = limits.max_diagnostics
    if len(diagnostics) < cap:
        return diagnostics
    omitted = diagnostics[cap - 1]
    return diagnostics[: cap - 1] + (
        InspectionDiagnostic(
            "LIMIT_REACHED",
            f"max_diagnostics cap {cap} exceeded while reporting {omitted.code}: {omitted.message}",
            omitted.source,
        ),
    )


def _project_diagnostics(root: LayoutNode) -> tuple[InspectionDiagnostic, ...]:
    """Project existing owner-local errors in occurrence preorder, without rediscovery.

    Derived subject conflicts resolve after traversal, but still appear at their
    source owner before its opaque children, not at the end of the result. The
    reserved traversal-termination marker remains the final diagnostic.
    """
    result: list[InspectionDiagnostic] = []
    stack: list[LayoutNode | ObjectNode] = [root]
    while stack:
        node = stack.pop()
        result.extend(node.errors)
        if isinstance(node, (IndexNode, ManifestNode)):
            if node.subject is not None:
                result.extend(node.subject.errors)
            if isinstance(node, IndexNode):
                descriptors = node.manifests or ()
            else:
                descriptors = (() if node.config is None else (node.config,)) + (
                    node.layers or ()
                )
            for descriptor in descriptors:
                result.extend(descriptor.errors)
        if isinstance(node, (LayoutNode, IndexNode, ManifestNode)):
            stack.extend(reversed(node.children or ()))
    return tuple(item for item in result if item.code != "LIMIT_REACHED") + tuple(
        item for item in result if item.code == "LIMIT_REACHED"
    )


class _Inspector:
    """Call-scoped safe handles, unique observations and independent traversal budgets."""

    def __init__(
        self, directory: SafeDirectory, limits: InspectionLimits, handles: ExitStack
    ) -> None:
        """Borrow the opened root and retain read-only child handles until call exit."""
        self.directory = directory
        self.limits = limits
        self.handles = handles
        self.directories: dict[str, SafeDirectory] = {}
        self.directory_failures: dict[str, _Problem] = {}
        self.observations: dict[str, _Observation] = {}
        self.diagnostics: list[InspectionDiagnostic] = []
        self.metadata_bytes = 0
        self.descriptors = 0
        self.object_references = 0
        self.subject_references = 0
        self.occurrences = 2
        self.stopped = False
        self.sha512_candidates: dict[str, str | None] = {}

    def _limit(self, budget: str, source: SourceLocation) -> _Problem:
        """Name the exact first over-cap attempted unit."""
        return _Problem(
            "LIMIT_REACHED",
            f"{budget} cap {getattr(self.limits, budget)} exceeded",
            source,
        )

    def _error(self, problem: _Problem, location: str | None) -> InspectionDiagnostic:
        """Activate an owner-local error, reserving the final diagnostic slot for a limit."""
        if len(self.diagnostics) >= self.limits.max_diagnostics - 1:
            problem = self._limit("max_diagnostics", problem.source)
        diagnostic = InspectionDiagnostic(
            problem.code, problem.message, problem.source, location
        )
        if len(self.diagnostics) >= self.limits.max_diagnostics:
            raise RuntimeError("Diagnostic reservation exhausted")
        self.diagnostics.append(diagnostic)
        if diagnostic.code == "LIMIT_REACHED":
            self.stopped = True
        return diagnostic

    def _activate(self, descriptor: DescriptorView, location: str) -> DescriptorView:
        """Attach cached descriptor failures to this occurrence, without revalidation IO."""
        if not descriptor.errors:
            return descriptor
        errors: list[InspectionDiagnostic] = []
        for item in descriptor.errors:
            if self.stopped:
                break
            errors.append(
                self._error(_Problem(item.code, item.message, item.source), location)
            )
        return replace(descriptor, errors=tuple(errors))

    def _read(
        self,
        directory: SafeDirectory,
        name: str,
        budget: str,
        source: SourceLocation,
        expected: os.stat_result | None = None,
    ) -> bytes:
        """Read cap-plus-one metadata bytes and compare entry/handle identity around IO."""
        cap = getattr(self.limits, budget)
        remaining = self.limits.max_total_metadata_bytes - self.metadata_bytes
        selected_budget = "max_total_metadata_bytes" if remaining < cap else budget
        cap = min(cap, remaining)
        try:
            with directory.read_file(name) as stream:
                before = os.fstat(stream.fileno())
                if expected is not None and _fingerprint(before) != _fingerprint(
                    expected
                ):
                    raise _InputFailure(
                        _Problem(
                            "CHANGED_DURING_READ",
                            "Metadata changed before safe read",
                            source,
                        )
                    )
                raw = bytearray()
                while len(raw) <= cap:
                    chunk = stream.read(min(65_536, cap + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                self.metadata_bytes += len(raw)
                if len(raw) > cap:
                    raise _InputFailure(self._limit(selected_budget, source))
                after = os.fstat(stream.fileno())
                entry = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                try:
                    directory.verify()
                except ArtifactError as error:
                    raise _InputFailure(
                        _Problem(
                            "CHANGED_DURING_READ",
                            "Metadata directory changed during read",
                            source,
                        )
                    ) from error
                if _fingerprint(before) != _fingerprint(after) or _fingerprint(
                    after
                ) != _fingerprint(entry):
                    raise _InputFailure(
                        _Problem(
                            "CHANGED_DURING_READ",
                            "Metadata changed during read",
                            source,
                        )
                    )
                return bytes(raw)
        except FileNotFoundError as error:
            raise _InputFailure(
                _Problem("MISSING_OBJECT", "Referenced file is missing", source)
            ) from error
        except (OSError, ArtifactError) as error:
            raise _InputFailure(
                _Problem(
                    "UNREADABLE_OBJECT", f"Cannot safely read metadata: {error}", source
                )
            ) from error

    def _blob_directory(self, algorithm: str, source: SourceLocation) -> SafeDirectory:
        """Cache checked read-only blob directories, including failed acquisition."""
        for name in ("blobs", f"blobs/{algorithm}"):
            if name in self.directory_failures:
                raise _InputFailure(
                    replace(self.directory_failures[name], source=source)
                )
            if name not in self.directories:
                parent = (
                    self.directory if name == "blobs" else self.directories["blobs"]
                )
                try:
                    self.directories[name] = self.handles.enter_context(
                        parent.open_child(name.rsplit("/", 1)[-1])
                    )
                except FileNotFoundError as error:
                    problem = _Problem(
                        "MISSING_OBJECT", "Referenced file is missing", source
                    )
                    self.directory_failures[name] = problem
                    raise _InputFailure(problem) from error
                except (OSError, ArtifactError) as error:
                    problem = _Problem(
                        "UNREADABLE_OBJECT",
                        f"Cannot safely open blob directory: {error}",
                        source,
                    )
                    self.directory_failures[name] = problem
                    raise _InputFailure(problem) from error
        return self.directories[f"blobs/{algorithm}"]

    def _observe(self, edge: DescriptorView, document_role: bool) -> _Observation:
        """Stat each target once and parse distinct supported metadata at most once."""
        digest = cast(str, edge.digest)
        algorithm, encoded = digest_parts(digest)
        location = f"blobs/{algorithm}/{encoded}"
        source = SourceLocation(location)
        if digest not in self.observations:
            if len(self.observations) >= self.limits.max_objects:
                raise _InputFailure(self._limit("max_objects", edge.source))
            observation = _Observation()
            self.observations[digest] = observation
            try:
                directory = self._blob_directory(algorithm, edge.source)
                directory.verify()
                identity = os.stat(encoded, dir_fd=directory.fd, follow_symlinks=False)
                if not stat.S_ISREG(identity.st_mode):
                    raise _InputFailure(
                        _Problem(
                            "UNREADABLE_OBJECT",
                            "Referenced input is not a regular file",
                            edge.source,
                        )
                    )
                observation.size = identity.st_size
                observation.identity = identity
            except FileNotFoundError:
                observation.problem = _Problem(
                    "MISSING_OBJECT", "Referenced file is missing", edge.source
                )
            except _InputFailure as error:
                observation.problem = error.problem
            except (OSError, ArtifactError) as error:
                observation.problem = _Problem(
                    "UNREADABLE_OBJECT",
                    f"Cannot safely stat referenced file: {error}",
                    edge.source,
                )
        observation = self.observations[digest]
        if (
            document_role
            and observation.problem is None
            and not observation.document_attempted
        ):
            observation.document_attempted = True
            try:
                directory = self._blob_directory(algorithm, edge.source)
                raw = self._read(
                    directory,
                    encoded,
                    "max_document_bytes",
                    source,
                    observation.identity,
                )
                parsed = _json(raw, self.limits, source)
                self._reserve_descriptors(parsed, source)
                observation.document = _document(parsed, source)
                self._collect_candidates(observation.document)
                self.object_references += (
                    len(observation.document.manifests or ())
                    + len(observation.document.layers or ())
                    + int(observation.document.config is not None)
                )
                self.subject_references += int(observation.document.subject is not None)
            except _InputFailure as error:
                observation.document_problem = (
                    _Problem(
                        "CHANGED_DURING_READ",
                        "Observed metadata disappeared during read",
                        source,
                    )
                    if error.problem.code == "MISSING_OBJECT"
                    else error.problem
                )
        return observation

    def _reserve_descriptors(
        self, parsed: dict[str, object], source: SourceLocation
    ) -> None:
        """Count source slots once per parsed document, including invalid/subject slots."""
        containment = int("config" in parsed)
        for key in ("manifests", "layers"):
            array = parsed.get(key)
            if isinstance(array, list):
                containment += len(array)
        subjects = int("subject" in parsed)
        count = containment + subjects
        if self.descriptors + count > self.limits.max_descriptors:
            raise _InputFailure(self._limit("max_descriptors", source))
        self.descriptors += count

    def _collect_candidates(self, document: _Document) -> None:
        """Cache SHA-512 prefix uniqueness only after intrinsic document validation.

        Subjects contribute source evidence without any subject-target IO.
        Repeated identical candidates retain uniqueness; collisions stay ambiguous.
        """
        for descriptor in chain(
            document.manifests or (),
            document.layers or (),
            (document.config,) if document.config is not None else (),
            (document.subject,) if document.subject is not None else (),
        ):
            digest = descriptor.digest
            if digest is not None and digest.startswith("sha512:"):
                prefix = digest[7:71]
                if prefix not in self.sha512_candidates:
                    self.sha512_candidates[prefix] = digest
                elif self.sha512_candidates[prefix] != digest:
                    self.sha512_candidates[prefix] = None

    def _prepare(self, pending: _Pending, document: _Document) -> None:
        """Install shared fields and source slots, activating only this owner's errors."""
        node = pending.node
        subject = document.subject
        if subject is not None:
            subject = self._activate(subject, node.location)
        if isinstance(node, IndexNode):
            descriptors = document.manifests or ()
            activated_list: list[DescriptorView] = []
            for item in descriptors:
                if self.stopped:
                    break
                activated_list.append(self._activate(item, node.location))
            activated = tuple(activated_list)
            pending.node = replace(
                node,
                subject=subject,
                manifests=activated,
                children=(),
                schema_version=2,
                media_type=document.media_type,
                artifact_type=document.artifact_type,
                annotations=document.annotations,
            )
            pending.slots = tuple(
                (item, "document", position) for position, item in enumerate(activated)
            )
        elif isinstance(node, ManifestNode):
            if self.stopped:
                pending.node = replace(
                    node, errors=node.errors + cast(DescriptorView, subject).errors
                )
                return
            config = cast(DescriptorView, document.config)
            config = (
                self._activate(config, node.location) if not self.stopped else config
            )
            layers = document.layers or ()
            activated_list = []
            for item in layers:
                if self.stopped:
                    break
                activated_list.append(self._activate(item, node.location))
            activated = tuple(activated_list)
            pending.node = replace(
                node,
                subject=subject,
                config=config,
                layers=activated,
                children=(),
                schema_version=2,
                media_type=document.media_type,
                artifact_type=document.artifact_type,
                annotations=document.annotations,
            )
            role = "content" if document.artifact_type else "layer"
            pending.slots = ((config, "config", None),) + tuple(
                (item, role, position) for position, item in enumerate(activated)
            )

    def _child(
        self, edge: DescriptorView, role: str, position: int | None, parent: _Pending
    ) -> _Pending:
        """Construct one target occurrence and compare its descriptor with cached facts."""
        digest = cast(str, edge.digest)
        algorithm, encoded = digest_parts(digest)
        location = f"blobs/{algorithm}/{encoded}"
        known_document = role == "document" and edge.media_type in DOCUMENT_TYPES
        identity: _Identity = {
            "location": location,
            "digest": digest,
            "source": edge.source,
            "incoming": edge,
            "source_position": position,
        }
        node: ObjectNode
        if role != "document":
            node = OpaqueNode(
                kind=cast(Literal["config", "layer", "content"], role),
                position=position,
                **identity,
            )
        elif edge.media_type in INDEX_TYPES:
            node = IndexNode(**identity)
        elif edge.media_type in MANIFEST_TYPES:
            node = ManifestNode(**identity)
        else:
            node = UnknownNode(**identity)
        child = _Pending(node, parent.depth + 1, parent.ancestors | {digest})
        try:
            observation = self._observe(edge, known_document)
        except _InputFailure as error:
            child.node = replace(node, errors=(self._error(error.problem, location),))
            return child
        problems: list[_Problem] = []
        if observation.problem is not None:
            problems.append(replace(observation.problem, source=edge.source))
        else:
            if edge.size is not None and observation.size != edge.size:
                problems.append(
                    _Problem(
                        "CONFLICTING_DESCRIPTOR",
                        "Descriptor size differs from observed file size",
                        edge.source,
                    )
                )
            if role == "document" and not known_document:
                problems.append(
                    _Problem(
                        "UNSUPPORTED_TYPE",
                        "Unsupported required descriptor media type",
                        edge.source,
                    )
                )
            elif known_document:
                document = observation.document
                if observation.document_problem is not None:
                    problems.append(observation.document_problem)
                    if observation.document_problem.code == "UNSUPPORTED_TYPE":
                        node = UnknownNode(**identity)
                elif document is not None:
                    if (
                        document.media_type is not None
                        and document.media_type != edge.media_type
                    ):
                        problems.append(
                            _Problem(
                                "CONFLICTING_DESCRIPTOR",
                                "Descriptor mediaType differs from document mediaType",
                                edge.source,
                            )
                        )
                    if (
                        edge.artifact_type is not None
                        and document.artifact_type is not None
                        and edge.artifact_type != document.artifact_type
                    ):
                        problems.append(
                            _Problem(
                                "CONFLICTING_DESCRIPTOR",
                                "Descriptor artifactType differs from document artifactType",
                                edge.source,
                            )
                        )
                    node = (
                        IndexNode(**identity)
                        if document.kind == "index"
                        else ManifestNode(**identity)
                    )
        if known_document and digest in parent.ancestors:
            problems.append(
                _Problem(
                    "CYCLE", "Containment target is an active ancestor", edge.source
                )
            )
        errors: list[InspectionDiagnostic] = []
        for problem in problems:
            if self.stopped:
                break
            errors.append(self._error(problem, location))
        child.node = replace(node, errors=tuple(errors))
        if (
            known_document
            and observation.document is not None
            and digest not in parent.ancestors
            and not self.stopped
        ):
            self._prepare(child, observation.document)
        return child

    def build(self, entry: _Document, location: str) -> LayoutInspectionResult:
        """Expand source containment iteratively, then freeze occurrence-local evidence."""
        root = _Pending(
            IndexNode(location="index.json", source=SourceLocation("index.json")),
            1,
            frozenset(),
        )
        self._prepare(root, entry)
        stack = [root]
        finished: list[_Pending] = []
        while stack:
            owner = stack[-1]
            if self.stopped or owner.next_slot >= len(owner.slots):
                stack.pop()
                finished.append(owner)
                continue
            edge, role, position = owner.slots[owner.next_slot]
            owner.next_slot += 1
            if edge.digest is None:
                continue
            budget = (
                "max_depth"
                if owner.depth + 1 > self.limits.max_depth
                else "max_occurrences"
                if self.occurrences >= self.limits.max_occurrences
                else None
            )
            if budget is not None:
                error = self._error(
                    self._limit(budget, edge.source), owner.node.location
                )
                owner.node = replace(owner.node, errors=owner.node.errors + (error,))
                continue
            self.occurrences += 1
            child = self._child(edge, role, position, owner)
            owner.children.append(child)
            stack.append(child)
        # All candidates are known before resolving sha512 prefix evidence.
        for owner in finished:
            node = owner.node
            if (
                isinstance(node, (IndexNode, ManifestNode))
                and node.children is not None
            ):
                children = tuple(child.node for child in owner.children)
                if isinstance(node, IndexNode):
                    node = replace(node, children=children)
                    evidence = self._association(node)
                    if evidence is not None:
                        node = replace(node, referrer_list=evidence)
                        if evidence.digest is not None and not self.stopped:
                            corrected = list(children)
                            for child_position, member in enumerate(children):
                                if (
                                    isinstance(member, (IndexNode, ManifestNode))
                                    and member.subject is not None
                                    and member.subject.digest is not None
                                    and member.subject.digest != evidence.digest
                                ):
                                    error = self._error(
                                        _Problem(
                                            "CONFLICTING_DESCRIPTOR",
                                            "Explicit member subject differs from tag-schema list subject",
                                            member.subject.source,
                                        ),
                                        member.location,
                                    )
                                    corrected[child_position] = replace(
                                        member,
                                        subject=replace(
                                            member.subject,
                                            errors=member.subject.errors + (error,),
                                        ),
                                    )
                                    if self.stopped:
                                        break
                            node = replace(node, children=tuple(corrected))
                else:
                    node = replace(
                        node, children=cast(tuple[OpaqueNode, ...], children)
                    )
                owner.node = node
        sizes = MappingProxyType(
            {
                digest: observation.size
                for digest, observation in self.observations.items()
                if observation.size is not None
            }
        )
        layout = LayoutNode(location, "1.0.0", (cast(IndexNode, root.node),))
        return LayoutInspectionResult(
            layout,
            _project_diagnostics(layout),
            self.limits,
            _StorageFacts(sizes, self.object_references, self.subject_references),
        )

    def _association(self, node: IndexNode) -> ReferrerListEvidence | None:
        """Recognize only tagged entry-point occurrences targeting declared OCI indexes."""
        incoming = node.incoming
        if (
            incoming is None
            or incoming.source.document != "index.json"
            or not incoming.source.pointer.startswith("/manifests/")
            or incoming.media_type != OCI_INDEX
            or node.media_type not in (None, OCI_INDEX)
            or node.schema_version is None
            or incoming.annotations is None
        ):
            return None
        tag = incoming.annotations.get(REF_NAME)
        match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match is None:
            return None
        algorithm, prefix = match.groups()
        digest = f"sha256:{prefix}" if algorithm == "sha256" else None
        if algorithm == "sha512":
            digest = self.sha512_candidates.get(prefix)
        return ReferrerListEvidence(
            cast(str, tag),
            SourceLocation(
                "index.json", incoming.source.pointer + "/annotations/" + REF_NAME
            ),
            cast(Literal["sha256", "sha512"], algorithm),
            prefix,
            "resolved" if digest is not None else "unresolved",
            digest,
        )


def inspect_layout(
    path: str | Path, *, limits: InspectionLimits | None = None
) -> LayoutInspectionResult:
    """Inspect a stable local directory without reading opaque payloads or writing files.

    :param path: Explicit directory, without parent traversal or symlink components.
    :param limits: Independent validated budgets; None selects inclusive defaults.
    :returns: Immutable source-ordered hierarchy, incomplete for descendant failures.
    :raises ValueError: For invalid arguments/budgets, before filesystem access.
    :raises LayoutInspectionError: For invalid roots or unsupported safe-open platforms.
    """
    if (
        not isinstance(path, (str, Path))
        or (isinstance(path, str) and not path)
        or "\x00" in str(path)
    ):
        raise ValueError("path must be a nonempty local string or Path")
    if limits is not None and not isinstance(limits, InspectionLimits):
        raise ValueError("limits must be InspectionLimits or None")
    policy = limits if limits is not None else InspectionLimits()
    source = SourceLocation("index.json")
    try:
        if sys.platform not in ("linux", "darwin"):
            raise ArtifactError(
                "Inspection supports Linux/macOS safe read-only filesystems"
            )
        require_readonly_filesystem()
    except ArtifactError as error:
        raise LayoutInspectionError(
            _root_diagnostics(
                (InspectionDiagnostic("UNSUPPORTED_PLATFORM", str(error), source),),
                policy,
            )
        ) from error
    try:
        with ExitStack() as handles:
            directory = handles.enter_context(SafeDirectory.open(path, read_only=True))
            inspector = _Inspector(directory, policy, handles)
            if policy.max_occurrences < 2:
                raise _InputFailure(inspector._limit("max_occurrences", source))
            marker_source = SourceLocation("oci-layout")
            try:
                marker = _json(
                    inspector._read(
                        directory,
                        "oci-layout",
                        "max_layout_marker_bytes",
                        marker_source,
                    ),
                    policy,
                    marker_source,
                )
            except _InputFailure as error:
                if error.problem.code == "MISSING_OBJECT":
                    raise _InputFailure(
                        _Problem(
                            "INVALID_LAYOUT",
                            "Required layout marker oci-layout is missing",
                            marker_source,
                        )
                    ) from error
                raise
            if marker.get("imageLayoutVersion") != "1.0.0":
                raise _InputFailure(
                    _Problem(
                        "INVALID_LAYOUT",
                        "imageLayoutVersion must be 1.0.0",
                        marker_source,
                    )
                )
            parsed = _json(
                inspector._read(directory, "index.json", "max_document_bytes", source),
                policy,
                source,
            )
            inspector._reserve_descriptors(parsed, source)
            entry = _document(parsed, source)
            if entry.kind != "index":
                raise _InputFailure(
                    _Problem("INVALID_LAYOUT", "Entry point must be an index", source)
                )
            inspector._collect_candidates(entry)
            containment = len(entry.manifests or ())
            inspector.object_references += containment
            inspector.subject_references += int(entry.subject is not None)
            result = inspector.build(entry, os.path.abspath(path))
            return result
    except _InputFailure as error:
        problem = error.problem
        diagnostics: tuple[InspectionDiagnostic, ...] = (
            InspectionDiagnostic("INVALID_LAYOUT", problem.message, problem.source),
        )
        if problem.code != "INVALID_LAYOUT":
            diagnostics += (
                InspectionDiagnostic(problem.code, problem.message, problem.source),
            )
        raise LayoutInspectionError(_root_diagnostics(diagnostics, policy)) from error
    except (OSError, ArtifactError) as error:
        raise LayoutInspectionError(
            _root_diagnostics(
                (
                    InspectionDiagnostic(
                        "INVALID_LAYOUT",
                        f"Cannot safely inspect layout: {error}",
                        source,
                    ),
                ),
                policy,
            )
        ) from error
