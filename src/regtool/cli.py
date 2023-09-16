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
import requests

from regtool.utils import dockercredstore

log = logging.getLogger(__name__)

@click.group()
def cli():
    pass

@click.command()
@click.option('-u','--username', required=False,
              prompt='Username', help="Username for the registry.")
@click.option('-p', '--password', required=False,
              prompt='Password', help="Password for the registry.",
              hide_input=True, confirmation_prompt=True)
@click.option('-r', '--registry', required=False,
              prompt='Registry', help="The OCI registry to login to.")
@click.option('-l', '--list', required=False, is_flag=True,
              help="List OCI logins stored in the credstore.")
def login(username, password=None, registry=None, list=False):
    """
    Authenticate with the OCI registry. This command requires Docker credential
    helper configured on the machine.
    """

    print(f"Username: {username} -> Password: {password} -> Registry: {registry}")
    if list == True:
        creds = dockercredstore.list()
        print(creds)
    else:
        creds = dockercredstore.get(registry=registry)
    print(creds)


cli.add_command(login)

if __name__ == '__main__':
    cli()