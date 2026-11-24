#!/usr/bin/env python3

"""
:mod: `errors` - Module defining all errors returned by the libraries
=====================================================================

    module:: errors
    :platform: Unix, Windows
    :synopsis: Module defining all errors and exceptions returned by the libraries
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from regshape.libs.layout.inspection_models import InspectionDiagnostic

class RegShapeError(Exception):
    """
    A base error class from which all other errors inheirt.

    It is not recommended to do a generic catch for this class but to handle
    individual errors.
    """
    def __init__(self, message: str = None, cause: str = None, *args: object) -> None:
        self.message = f"{message} : {cause}"
        super().__init__(self.message, *args)

class AuthError(RegShapeError):
    """
    Error caused by authentication failure.
    """
    pass


class CredentialStoreError(AuthError):
    """Credential persistence failed after successful login verification.

    :param error: The original storage-layer authentication error.
    :ivar error: Original error, retained for storage-specific CLI rendering.
    """

    def __init__(self, error: AuthError) -> None:
        """Retain the storage error and add the failed operation's context."""
        self.error = error
        super().__init__("Could not store credentials", str(error))


class ManifestError(RegShapeError):
    """
    Error caused by a malformed, unknown, or unprocessable manifest.
    """
    pass


class ReferenceValidationError(ValueError):
    """A reference violates a library operation's input contract before HTTP.

    Unlike registry/domain failures, this is a caller usage error. The CLI
    renders its message unchanged and maps it to exit code 2.
    """


class UnsafeReferenceError(ValueError):
    """Reference parsing rejected URL/userinfo syntax or unsafe characters.

    Messages omit the supplied value. Renderers must also omit the raw input
    from their diagnostic context; this error does not prescribe an exit code.
    """


class TagError(RegShapeError):
    """
    Error caused by a malformed or unprocessable tag-list response.
    """
    pass


class BlobError(RegShapeError):
    """
    Error caused by a malformed, missing, or unprocessable blob or upload session.
    """

    def __init__(self, message: str = None, cause: str = None, *args: object,
                 status_code: int = None) -> None:
        super().__init__(message, cause, *args)
        self.status_code = status_code


class CatalogError(RegShapeError):
    """
    Error caused by a malformed or unprocessable catalog response.
    """
    pass


class CatalogNotSupportedError(CatalogError):
    """
    Error raised when the registry does not implement the catalog endpoint.

    Raised by the operations layer when ``GET /v2/_catalog`` returns an HTTP
    status that indicates the endpoint is not available (for example,
    ``404`` or ``405``). Callers can catch this subclass separately to
    distinguish "endpoint not available" from "response was malformed".
    """
    pass


class ReferrerError(RegShapeError):
    """
    Error caused by a malformed or unprocessable referrers response.
    """
    pass


class LayoutError(RegShapeError):
    """
    Error caused by an invalid or inconsistent OCI Image Layout on disk.
    """
    pass


class ArtifactError(RegShapeError):
    """A graph, resource-budget, safe-storage or artifact-publication failure."""


class LayoutInspectionError(ArtifactError):
    """No-tree root/platform failure or refusal to materialize oversized output.

    ``diagnostics`` retains nonempty typed causes; no empty success result or
    automatic metadata logging is produced.
    """

    def __init__(self, diagnostics: tuple["InspectionDiagnostic", ...]) -> None:
        """Preserve typed inspection causes without legacy absent-cause text."""
        if not diagnostics:
            raise ValueError("LayoutInspectionError requires diagnostics")
        self.diagnostics = diagnostics
        self.message = "; ".join(f"{item.code}: {item.message}" for item in diagnostics)
        Exception.__init__(self, self.message)


class ComponentError(ArtifactError):
    """Invalid component selection, destination or operation-owned filesystem action.

    Parser, native-storage, receiver and caller-observer errors retain their
    established types. Existing-output conflicts never authorize replacement.
    """

    def __init__(self, message: str | None = None, cause: str | None = None, *args: object) -> None:
        """Retain component detail without appending an absent legacy cause.

        Explicit cause text is preserved; chained exceptions remain separate
        from the caller-facing diagnostic.
        """
        self.message = str(message) if cause is None else f"{message} : {cause}"
        Exception.__init__(self, self.message, *args)


class DockerError(RegShapeError):
    """
    Error caused by a Docker daemon interaction failure.
    """
    pass


class PingError(RegShapeError):
    """A failed registry ping, with a caller-ready transport diagnostic.

    The inherited message and exception chain remain unchanged; ``detail``
    selects the direct cause's text when present.
    """

    @property
    def detail(self) -> str:
        """Return the chained cause's text, or this error's text if unchained."""
        return str(self.__cause__) if self.__cause__ is not None else str(self)