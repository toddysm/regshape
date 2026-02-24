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