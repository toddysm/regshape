#!/usr/bin/env python3

"""
:mod: `errors` - Module defining all errors returned by the tools
=================================================================

    module:: errors
    :platform: Unix, Windows
    :synopsis: Module defining all errors and exceptions returned by the tools
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

class RegistryToolError(Exception):
    """
    A base error class from which all other errors inheirt.

    It is not recommended to do a generic catch for this class but to handle
    individual errors.
    """
    def __init__(self, message: str = None, cause: str = None, *args: object) -> None:
        self.message = f"{message} : {cause}"
        super().__init__(self.message, *args)

class AuthError(RegistryToolError):
    """
    Error caused by authentication failure.
    """
    pass