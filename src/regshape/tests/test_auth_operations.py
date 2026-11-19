"""Verify SDK authentication outcomes, configuration and persistence ordering."""

import json
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from regshape.libs.auth import login, verify_credentials
from regshape.libs.errors import AuthError, CredentialStoreError

REGISTRY = "registry.example.com"


@pytest.mark.parametrize("operation", [verify_credentials, login])
@pytest.mark.parametrize(
    ("username", "password"), [(None, None), (None, "token"), ("alice", None)]
)
def test_missing_credentials_rejected_before_io(
    operation, username, password, tmp_path
):
    """Missing explicit credentials cannot resolve or overwrite stored credentials."""
    config_path = tmp_path / "config.json"
    original_config = json.dumps({"auths": {REGISTRY: {"auth": "Z29vZDpzZWNyZXQ="}}})
    config_path.write_text(original_config)
    options = {"docker_config_path": str(config_path)} if operation is login else {}

    with (
        patch("regshape.libs.transport.client.RegistryClient") as factory,
        patch("regshape.libs.auth.credentials.dockerconfig.load_config") as load,
        patch("regshape.libs.auth.credentials.store_credentials") as store,
        pytest.raises(AuthError, match="username and password must not be None"),
    ):
        operation(REGISTRY, username, password, **options)

    factory.assert_not_called()
    load.assert_not_called()
    store.assert_not_called()
    assert config_path.read_text() == original_config


@pytest.mark.parametrize("options", [{}, {"insecure": True, "timeout": 27}])
def test_verify_credentials_success(options):
    """A 200 response succeeds without storage and forwards connection settings."""
    with (
        patch("regshape.libs.transport.client.RegistryClient") as factory,
        patch("regshape.libs.auth.credentials.store_credentials") as store,
    ):
        response = factory.return_value.get.return_value
        response.status_code = 200

        assert verify_credentials(REGISTRY, "alice", "token", **options) is None

    config = factory.call_args.args[0]
    assert (config.registry, config.username, config.password) == (
        REGISTRY,
        "alice",
        "token",
    )
    assert config.insecure == options.get("insecure", False)
    assert config.timeout == options.get("timeout", 10)
    factory.return_value.get.assert_called_once_with("/v2/")
    response.close.assert_called_once_with()
    store.assert_not_called()


@pytest.mark.parametrize("status", [201, 401, 403, 500])
def test_verify_credentials_rejects_non_200(status):
    """Even a different successful HTTP status is rejected and its response closed."""
    with patch("regshape.libs.transport.client.RegistryClient") as factory:
        response = factory.return_value.get.return_value
        response.status_code = status

        with pytest.raises(AuthError) as caught:
            verify_credentials(REGISTRY, "alice", "token")

    assert str(caught.value) == (
        f"Login failed : authentication rejected (status {status})"
    )
    response.close.assert_called_once_with()


@pytest.mark.parametrize(
    "error",
    [
        AuthError("Authentication failed", "token rejected"),
        requests.exceptions.ConnectionError("refused"),
    ],
)
def test_verify_credentials_propagates_errors(error):
    """Authentication and transport failures retain their original identity."""
    with patch("regshape.libs.transport.client.RegistryClient") as factory:
        factory.return_value.get.side_effect = error
        with pytest.raises(type(error)) as caught:
            verify_credentials(REGISTRY, "alice", "token")

    assert caught.value is error


def test_login_verifies_before_storing():
    """Login stores the same explicit credentials only after verification returns."""
    calls = MagicMock()
    with (
        patch("regshape.libs.auth.credentials.verify_credentials") as verify,
        patch("regshape.libs.auth.credentials.store_credentials") as store,
    ):
        calls.attach_mock(verify, "verify")
        calls.attach_mock(store, "store")

        assert (
            login(
                REGISTRY,
                "alice",
                "token",
                insecure=True,
                timeout=27,
                docker_config_path="custom-config.json",
            )
            is None
        )

    assert calls.mock_calls == [
        call.verify(REGISTRY, "alice", "token", insecure=True, timeout=27),
        call.store(
            REGISTRY,
            "alice",
            "token",
            docker_config_path="custom-config.json",
        ),
    ]


@pytest.mark.parametrize(
    "error",
    [
        AuthError("Login failed", "rejected"),
        requests.exceptions.Timeout("timed out"),
    ],
)
def test_login_does_not_store_failed_credentials(error):
    """Verification failure is propagated without invoking any credential storage."""
    with (
        patch("regshape.libs.auth.credentials.verify_credentials", side_effect=error),
        patch("regshape.libs.auth.credentials.store_credentials") as store,
        pytest.raises(type(error)) as caught,
    ):
        login(REGISTRY, "alice", "token")

    assert caught.value is error
    store.assert_not_called()


def test_login_reports_storage_failure():
    """A storage failure remains an AuthError with the original error and cause."""
    error = AuthError("store failed", "helper unavailable")
    with (
        patch("regshape.libs.auth.credentials.verify_credentials"),
        patch("regshape.libs.auth.credentials.store_credentials", side_effect=error),
        pytest.raises(CredentialStoreError) as caught,
    ):
        login(REGISTRY, "alice", "token")

    assert isinstance(caught.value, AuthError)
    assert caught.value.error is error
    assert caught.value.__cause__ is error
    assert str(caught.value) == (
        "Could not store credentials : store failed : helper unavailable"
    )
