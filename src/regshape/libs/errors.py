#!/usr/bin/env python3

"""
:mod: `errors` - Module defining all errors returned by the libraries
=====================================================================

    module:: errors
    :platform: Unix, Windows
    :synopsis: Module defining all errors and exceptions returned by the libraries
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

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


class ManifestError(RegShapeError):
    """
    Error caused by a malformed, unknown, or unprocessable manifest.
    """
    pass


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