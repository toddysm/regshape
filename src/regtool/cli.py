#!/usr/bin/env python3

"""
:mod: `cli` - Module for the registrytool CLI
=============================================

    module:: cli
    :platform: Unix, Windows
    :synopsis: Module for the registrytool CLI. Has a function for each CLI
            command.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import click

import logging

log = logging.getLogger(__name__)

@click.group()
def cli():
    pass

@click.command()
@click.option("--username", prompt="Username", help="Username for the registry.")
@click.option("--password", prompt="Password", help="Password for the registry.")
@click.option("--registry", prompt="Registry", help="The OCI registry to login to.")
def login(username, password=None, registry=None):
    """
    Authenticate with the OCI registry. This command requires Docker credential
    helper configured on the machine.
    """
    print(f"Username: {username} -> Password: {password} -> Registry: {registry}")

cli.add_command(login)

if __name__ == '__main__':
    cli()