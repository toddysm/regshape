#!/usr/bin/env python3

"""
:mod: `test_auth` - Module for testing RegShape's authentication package
=========================================================================

    module:: test_auth
    :platform: Unix, Windows
    :synopsis: Module for testing RegShape's authentication package
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

from regshape.lib.auth import dockercredstore

def test_credstore_access():
    assert dockercredstore.list() != None
