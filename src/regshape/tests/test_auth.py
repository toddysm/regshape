#!/usr/bin/env python3

"""
:mod: `test_auth` - Test suite for regshape.libs.auth
=======================================================

    module:: test_auth
    :platform: Unix, Windows
    :synopsis: Unit tests for registryauth, dockerconfig, and dockercredstore.
    moduleauthor:: ToddySM <toddysm@gmail.com>

Tests for:
  - registryauth  (_parse_auth_header, _get_basic_auth, _get_auth_token, authenticate)
  - dockerconfig  (home_dir, config_path_from_env, get_config_file, load_config)
  - dockercredstore (list, get, erase, store)

Two tests are marked xfail because they document correct intended behaviour
for known bugs. They will transition to passing once those bugs are fixed:
  - TestAuthenticate.test_bearer_scheme_returns_token_string
  - TestCredstoreStore.test_sends_credentials_as_single_json_payload
"""

import base64
import json
import os
import pytest
import requests
from unittest.mock import MagicMock, patch

from regshape.libs.auth import dockerconfig, dockercredstore, registryauth
from regshape.libs.errors import AuthError


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _bearer_header(realm='https://auth.example.com/token',
                   service='registry.example.com',
                   scope=None):
    """Build a Bearer WWW-Authenticate header string."""
    header = f'Bearer realm="{realm}",service="{service}"'
    if scope:
        header += f',scope="{scope}"'
    return header


def _token_response(token='test-token'):
    """Return a mock requests.Response for a token endpoint."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = json.dumps({'token': token, 'access_token': token})
    return mock_resp


def _popen_mock(stdout: bytes, returncode: int = 0):
    """Return a Popen mock whose communicate() yields (stdout, b'')."""
    proc = MagicMock()
    proc.communicate.return_value = (stdout, b'')
    proc.returncode = returncode
    return proc


# ===========================================================================
# registryauth._parse_auth_header
# ===========================================================================

class TestParseAuthHeader:

    def test_basic_scheme(self):
        header = 'Basic realm="https://registry.example.com"'
        result = registryauth._parse_auth_header(header)
        assert result['scheme'] == 'Basic'
        assert result['realm'] == 'https://registry.example.com'

    def test_bearer_scheme_with_realm_service_scope(self):
        header = _bearer_header(scope='repository:library/ubuntu:pull')
        result = registryauth._parse_auth_header(header)
        assert result['scheme'] == 'Bearer'
        assert result['realm'] == 'https://auth.example.com/token'
        assert result['service'] == 'registry.example.com'
        assert result['scope'] == 'repository:library/ubuntu:pull'

    def test_bearer_scheme_without_scope(self):
        header = _bearer_header()
        result = registryauth._parse_auth_header(header)
        assert result['scheme'] == 'Bearer'
        assert 'scope' not in result

    def test_scheme_is_first_whitespace_token(self):
        header = 'Bearer realm="https://auth.example.com/token"'
        result = registryauth._parse_auth_header(header)
        assert result['scheme'] == 'Bearer'

    def test_realm_url_with_query_param_containing_equals(self):
        """A realm URL with ?key=value must not be split on the inner '='."""
        header = (
            'Bearer realm="https://auth.example.com/token?service=reg",'
            'service="registry.example.com"'
        )
        result = registryauth._parse_auth_header(header)
        assert result['realm'] == 'https://auth.example.com/token?service=reg'
        assert result['service'] == 'registry.example.com'


# ===========================================================================
# registryauth._get_basic_auth
# ===========================================================================

class TestGetBasicAuth:

    def test_encodes_username_and_password(self):
        result = registryauth._get_basic_auth('alice', 's3cr3t')
        expected = base64.b64encode(b'alice:s3cr3t').decode('utf-8')
        assert result == expected

    def test_returns_string(self):
        assert isinstance(registryauth._get_basic_auth('user', 'pass'), str)

    def test_colon_in_password_is_preserved(self):
        """Only the first colon separates user from password."""
        result = registryauth._get_basic_auth('user', 'p:a:s:s')
        expected = base64.b64encode(b'user:p:a:s:s').decode('utf-8')
        assert result == expected

    def test_special_characters_in_credentials(self):
        result = registryauth._get_basic_auth('user@domain.com', 'p@$$w0rd!')
        expected = base64.b64encode(b'user@domain.com:p@$$w0rd!').decode('utf-8')
        assert result == expected


# ===========================================================================
# registryauth._get_auth_token
# ===========================================================================

class TestGetAuthToken:

    def _header(self, realm='https://auth.example.com/token',
                service='registry.example.com', scope=None):
        h = {'scheme': 'Bearer', 'realm': realm, 'service': service}
        if scope:
            h['scope'] = scope
        return h

    def test_makes_authenticated_request_when_credentials_supplied(self):
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response('tok')
            registryauth._get_auth_token(
                self._header(scope='repository:myrepo:pull'), 'user', 'pass'
            )
        _, kwargs = mock_get.call_args
        assert kwargs['auth'] == ('user', 'pass')

    def test_makes_anonymous_request_without_credentials(self):
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response('tok')
            registryauth._get_auth_token(self._header())
        _, kwargs = mock_get.call_args
        assert 'auth' not in kwargs

    def test_realm_used_as_request_url(self):
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response()
            registryauth._get_auth_token(
                self._header(realm='https://auth.custom.io/token'), 'u', 'p'
            )
        url = mock_get.call_args[0][0]
        assert url == 'https://auth.custom.io/token'

    def test_service_and_scope_sent_as_query_params(self):
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response()
            registryauth._get_auth_token(
                self._header(scope='repository:myrepo:pull,push'), 'u', 'p'
            )
        params = mock_get.call_args[1]['params']
        assert params['service'] == 'registry.example.com'
        assert params['scope'] == 'repository:myrepo:pull,push'

    def test_scope_omitted_from_params_when_absent(self):
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response()
            registryauth._get_auth_token(self._header(), 'u', 'p')
        params = mock_get.call_args[1]['params']
        assert 'scope' not in params

    def test_raises_auth_error_when_realm_missing(self):
        header = {'scheme': 'Bearer', 'service': 'registry.example.com'}
        with pytest.raises(AuthError):
            registryauth._get_auth_token(header)

    def test_raises_auth_error_when_service_missing(self):
        header = {'scheme': 'Bearer', 'realm': 'https://auth.example.com/token'}
        with pytest.raises(AuthError):
            registryauth._get_auth_token(header)

    def test_raises_auth_error_on_non_200_status(self):
        header = self._header()
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=401, text='{"errors":[]}')
            with pytest.raises(AuthError):
                registryauth._get_auth_token(header, 'user', 'wrongpass')

    def test_raises_auth_error_on_connection_error(self):
        header = self._header()
        with patch('regshape.libs.auth.registryauth.requests.get',
                   side_effect=requests.exceptions.ConnectionError):
            with pytest.raises(AuthError):
                registryauth._get_auth_token(header)

    def test_raises_auth_error_on_timeout(self):
        header = self._header()
        with patch('regshape.libs.auth.registryauth.requests.get',
                   side_effect=requests.exceptions.Timeout):
            with pytest.raises(AuthError):
                registryauth._get_auth_token(header)

    def test_raises_auth_error_on_generic_request_exception(self):
        header = self._header()
        with patch('regshape.libs.auth.registryauth.requests.get',
                   side_effect=requests.exceptions.RequestException):
            with pytest.raises(AuthError):
                registryauth._get_auth_token(header)


# ===========================================================================
# registryauth.authenticate
# ===========================================================================

class TestAuthenticate:

    def test_basic_scheme_returns_base64_string(self):
        header = 'Basic realm="https://registry.example.com"'
        result = registryauth.authenticate(header, 'alice', 's3cr3t')
        assert result == base64.b64encode(b'alice:s3cr3t').decode('utf-8')

    def test_bearer_scheme_returns_token_string(self):
        header = _bearer_header(scope='repository:library/ubuntu:pull')
        with patch('regshape.libs.auth.registryauth.requests.get') as mock_get:
            mock_get.return_value = _token_response('bearer-token')
            result = registryauth.authenticate(header, 'user', 'pass')

        assert isinstance(result, str)
        assert result == 'bearer-token'

    def test_unknown_scheme_raises_auth_error(self):
        header = 'Digest realm="example.com",nonce="abc123"'
        with pytest.raises(AuthError):
            registryauth.authenticate(header)


# ===========================================================================
# dockerconfig
# ===========================================================================

class TestHomeDir:

    def test_posix_returns_home_directory(self):
        with patch('regshape.libs.auth.dockerconfig.IS_WINDOWS_PLATFORM', False):
            result = dockerconfig.home_dir()
        assert result == os.path.expanduser('~')

    def test_windows_returns_userprofile_env_var(self, monkeypatch):
        monkeypatch.setenv('USERPROFILE', r'C:\Users\testuser')
        with patch('regshape.libs.auth.dockerconfig.IS_WINDOWS_PLATFORM', True):
            result = dockerconfig.home_dir()
        assert result == r'C:\Users\testuser'


class TestConfigPathFromEnv:

    def test_returns_path_when_docker_config_is_set(self, monkeypatch):
        monkeypatch.setenv('DOCKER_CONFIG', '/custom/docker')
        assert dockerconfig.config_path_from_env() == '/custom/docker/config.json'

    def test_returns_none_when_env_var_not_set(self, monkeypatch):
        monkeypatch.delenv('DOCKER_CONFIG', raising=False)
        assert dockerconfig.config_path_from_env() is None


class TestGetConfigFile:

    def test_returns_explicit_path_when_it_exists(self, tmp_path):
        config = tmp_path / 'config.json'
        config.write_text('{}')
        assert dockerconfig.get_config_file(str(config)) == str(config)

    def test_falls_through_explicit_path_to_env_when_explicit_missing(
        self, tmp_path, monkeypatch
    ):
        env_dir = tmp_path / 'env_docker'
        env_dir.mkdir()
        (env_dir / 'config.json').write_text('{}')
        monkeypatch.setenv('DOCKER_CONFIG', str(env_dir))
        result = dockerconfig.get_config_file('/does/not/exist/config.json')
        assert result == str(env_dir / 'config.json')

    def test_returns_default_location_when_it_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv('DOCKER_CONFIG', raising=False)
        docker_dir = tmp_path / '.docker'
        docker_dir.mkdir()
        (docker_dir / 'config.json').write_text('{}')
        with patch('regshape.libs.auth.dockerconfig.home_dir', return_value=str(tmp_path)):
            result = dockerconfig.get_config_file()
        assert result == str(docker_dir / 'config.json')

    def test_returns_none_when_no_config_exists_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.delenv('DOCKER_CONFIG', raising=False)
        with patch('regshape.libs.auth.dockerconfig.home_dir', return_value=str(tmp_path)):
            result = dockerconfig.get_config_file()
        assert result is None


class TestLoadConfig:

    def test_returns_parsed_dict_for_valid_file(self, tmp_path):
        data = {'auths': {'registry.example.com': {'auth': 'dXNlcjpwYXNz'}}}
        config = tmp_path / 'config.json'
        config.write_text(json.dumps(data))
        assert dockerconfig.load_config(str(config)) == data

    def test_returns_none_when_no_config_file_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv('DOCKER_CONFIG', raising=False)
        with patch('regshape.libs.auth.dockerconfig.home_dir', return_value=str(tmp_path)):
            result = dockerconfig.load_config()
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        config = tmp_path / 'config.json'
        config.write_text('{ not valid json }}}')
        assert dockerconfig.load_config(str(config)) is None

    def test_returns_none_on_os_error(self, tmp_path):
        config = tmp_path / 'config.json'
        config.write_text('{}')
        with patch('builtins.open', side_effect=OSError('permission denied')):
            result = dockerconfig.load_config(str(config))
        assert result is None


# ===========================================================================
# dockercredstore
# ===========================================================================

class TestCredstoreList:

    def test_returns_credential_dict(self):
        creds = {'https://registry.example.com': 'alice'}
        proc = _popen_mock(json.dumps(creds).encode())
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            result = dockercredstore.list()
        assert result == creds

    def test_uses_specified_store_in_command(self):
        proc = _popen_mock(json.dumps({}).encode())
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc) as mock_popen:
            dockercredstore.list(store='pass')
        assert mock_popen.call_args[0][0] == ['docker-credential-pass', 'list']

    def test_raises_auth_error_when_helper_not_found(self):
        with patch('regshape.libs.auth.dockercredstore.Popen', side_effect=FileNotFoundError):
            with pytest.raises(AuthError):
                dockercredstore.list()

    def test_raises_auth_error_on_invalid_json(self):
        proc = _popen_mock(b'not valid json')
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            with pytest.raises(AuthError):
                dockercredstore.list()


class TestCredstoreGet:

    def test_returns_credentials_for_registry(self):
        creds = {'ServerURL': 'https://registry.example.com',
                 'Username': 'alice', 'Secret': 's3cr3t'}
        proc = _popen_mock(json.dumps(creds).encode())
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            result = dockercredstore.get(registry='https://registry.example.com')
        assert result == creds

    def test_sends_registry_as_stdin(self):
        creds = {'ServerURL': 'https://registry.example.com',
                 'Username': 'alice', 'Secret': 's3cr3t'}
        proc = _popen_mock(json.dumps(creds).encode())
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            dockercredstore.get(registry='https://registry.example.com')
        proc.communicate.assert_called_once_with(input=b'https://registry.example.com')

    def test_raises_auth_error_when_helper_not_found(self):
        with patch('regshape.libs.auth.dockercredstore.Popen', side_effect=FileNotFoundError):
            with pytest.raises(AuthError):
                dockercredstore.get(registry='https://registry.example.com')

    def test_raises_auth_error_on_invalid_json(self):
        proc = _popen_mock(b'not valid json')
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            with pytest.raises(AuthError):
                dockercredstore.get(registry='https://registry.example.com')

    def test_raises_auth_error_when_registry_is_none(self):
        with pytest.raises(AuthError):
            dockercredstore.get(registry=None)


class TestCredstoreErase:

    def test_sends_registry_as_stdin(self):
        proc = _popen_mock(b'')
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            dockercredstore.erase(registry='https://registry.example.com')
        proc.communicate.assert_called_once_with(input=b'https://registry.example.com')

    def test_uses_specified_store_in_command(self):
        proc = _popen_mock(b'')
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc) as mock_popen:
            dockercredstore.erase(store='pass', registry='https://registry.example.com')
        assert mock_popen.call_args[0][0] == ['docker-credential-pass', 'erase']

    def test_raises_auth_error_when_helper_not_found(self):
        with patch('regshape.libs.auth.dockercredstore.Popen', side_effect=FileNotFoundError):
            with pytest.raises(AuthError):
                dockercredstore.erase(registry='https://registry.example.com')

    def test_raises_auth_error_when_registry_is_none(self):
        with pytest.raises(AuthError):
            dockercredstore.erase(registry=None)


class TestCredstoreStore:

    def test_sends_credentials_as_single_json_payload(self):
        """
        The docker-credential-* store protocol expects exactly one communicate()
        call with a JSON body containing ServerURL, Username, and Secret.
        """
        credentials = {'Username': 'alice', 'Secret': 's3cr3t'}
        expected_payload = {
            'ServerURL': 'https://registry.example.com',
            'Username': 'alice',
            'Secret': 's3cr3t',
        }
        proc = _popen_mock(b'')
        with patch('regshape.libs.auth.dockercredstore.Popen', return_value=proc):
            dockercredstore.store(
                registry='https://registry.example.com',
                credentials=credentials,
            )
        proc.communicate.assert_called_once_with(
            input=json.dumps(expected_payload).encode('utf-8')
        )

    def test_raises_auth_error_when_helper_not_found(self):
        with patch('regshape.libs.auth.dockercredstore.Popen', side_effect=FileNotFoundError):
            with pytest.raises(AuthError):
                dockercredstore.store(
                    registry='https://registry.example.com',
                    credentials={'Username': 'u', 'Secret': 'p'},
                )

    def test_raises_auth_error_when_registry_is_none(self):
        with pytest.raises(AuthError):
            dockercredstore.store(registry=None, credentials={'Username': 'u', 'Secret': 'p'})

    def test_raises_auth_error_when_credentials_is_none(self):
        with pytest.raises(AuthError):
            dockercredstore.store(registry='https://registry.example.com', credentials=None)
