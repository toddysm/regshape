#!/usr/bin/env python3

"""Tests for :mod:`regshape.libs.models.error`."""

import json

import pytest
from unittest.mock import MagicMock

from regshape.libs.models.error import OciErrorDetail, OciErrorResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(body: str, status_code: int = 200) -> MagicMock:
    import requests
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = body
    return resp


_SINGLE_ERROR_JSON = json.dumps({
    "errors": [
        {"code": "MANIFEST_UNKNOWN", "message": "manifest unknown", "detail": {"Tag": "v1.0"}}
    ]
})

_MULTI_ERROR_JSON = json.dumps({
    "errors": [
        {"code": "UNAUTHORIZED",    "message": "authentication required"},
        {"code": "TOOMANYREQUESTS", "message": "rate limit exceeded", "detail": None},
    ]
})

_NO_ERRORS_JSON = json.dumps({"errors": []})
_NULL_ERRORS_JSON = json.dumps({"errors": None})
_MISSING_ERRORS_JSON = json.dumps({"message": "not found"})
_EMPTY_JSON = "{}"


# ===========================================================================
# TestOciErrorDetail
# ===========================================================================

class TestOciErrorDetailFromDict:

    def test_full_entry(self):
        d = OciErrorDetail.from_dict({
            "code": "MANIFEST_UNKNOWN",
            "message": "manifest unknown",
            "detail": {"Tag": "v1.0"},
        })
        assert d.code == "MANIFEST_UNKNOWN"
        assert d.message == "manifest unknown"
        assert d.detail == {"Tag": "v1.0"}

    def test_missing_code_defaults_to_empty(self):
        d = OciErrorDetail.from_dict({"message": "something went wrong"})
        assert d.code == ""
        assert d.message == "something went wrong"

    def test_missing_message_defaults_to_empty(self):
        d = OciErrorDetail.from_dict({"code": "NAME_UNKNOWN"})
        assert d.code == "NAME_UNKNOWN"
        assert d.message == ""

    def test_null_code_defaults_to_empty(self):
        d = OciErrorDetail.from_dict({"code": None, "message": "oops"})
        assert d.code == ""

    def test_null_message_defaults_to_empty(self):
        d = OciErrorDetail.from_dict({"code": "BLOB_UNKNOWN", "message": None})
        assert d.message == ""

    def test_missing_detail_is_none(self):
        d = OciErrorDetail.from_dict({"code": "X", "message": "y"})
        assert d.detail is None

    def test_null_detail_is_none(self):
        d = OciErrorDetail.from_dict({"code": "X", "message": "y", "detail": None})
        assert d.detail is None

    def test_list_detail_preserved(self):
        d = OciErrorDetail.from_dict({"code": "X", "message": "y", "detail": [1, 2]})
        assert d.detail == [1, 2]

    def test_string_detail_preserved(self):
        d = OciErrorDetail.from_dict({"code": "X", "message": "y", "detail": "extra info"})
        assert d.detail == "extra info"

    def test_not_a_dict_raises(self):
        with pytest.raises(ValueError, match="expected a dict"):
            OciErrorDetail.from_dict(["code", "message"])

    def test_string_raises(self):
        with pytest.raises(ValueError, match="expected a dict"):
            OciErrorDetail.from_dict("MANIFEST_UNKNOWN")


class TestOciErrorDetailFormat:

    def test_both_non_empty(self):
        d = OciErrorDetail(code="MANIFEST_UNKNOWN", message="manifest unknown")
        assert d.format() == "MANIFEST_UNKNOWN: manifest unknown"

    def test_code_only(self):
        d = OciErrorDetail(code="MANIFEST_UNKNOWN", message="")
        assert d.format() == "MANIFEST_UNKNOWN"

    def test_message_only(self):
        d = OciErrorDetail(code="", message="something went wrong")
        assert d.format() == "something went wrong"

    def test_both_empty(self):
        d = OciErrorDetail(code="", message="")
        assert d.format() == ""


class TestOciErrorDetailToDict:

    def test_detail_omitted_when_none(self):
        d = OciErrorDetail(code="X", message="y")
        result = d.to_dict()
        assert "detail" not in result
        assert result == {"code": "X", "message": "y"}

    def test_detail_included_when_present(self):
        d = OciErrorDetail(code="X", message="y", detail={"k": "v"})
        assert d.to_dict() == {"code": "X", "message": "y", "detail": {"k": "v"}}


class TestOciErrorDetailValidation:

    def test_non_str_code_raises(self):
        with pytest.raises(ValueError, match="code must be a str"):
            OciErrorDetail(code=123, message="msg")  # type: ignore

    def test_non_str_message_raises(self):
        with pytest.raises(ValueError, match="message must be a str"):
            OciErrorDetail(code="X", message=None)  # type: ignore


# ===========================================================================
# TestOciErrorResponse
# ===========================================================================

class TestOciErrorResponseFromDict:

    def test_single_error(self):
        r = OciErrorResponse.from_dict(json.loads(_SINGLE_ERROR_JSON))
        assert len(r.errors) == 1
        assert r.errors[0].code == "MANIFEST_UNKNOWN"

    def test_multiple_errors(self):
        r = OciErrorResponse.from_dict(json.loads(_MULTI_ERROR_JSON))
        assert len(r.errors) == 2
        assert r.errors[0].code == "UNAUTHORIZED"
        assert r.errors[1].code == "TOOMANYREQUESTS"

    def test_empty_errors_array(self):
        r = OciErrorResponse.from_dict(json.loads(_NO_ERRORS_JSON))
        assert r.errors == []

    def test_null_errors_normalised_to_empty(self):
        r = OciErrorResponse.from_dict(json.loads(_NULL_ERRORS_JSON))
        assert r.errors == []

    def test_missing_errors_key_normalised_to_empty(self):
        r = OciErrorResponse.from_dict(json.loads(_MISSING_ERRORS_JSON))
        assert r.errors == []

    def test_not_a_dict_raises(self):
        with pytest.raises(ValueError, match="expected a dict"):
            OciErrorResponse.from_dict([{"code": "X"}])

    def test_malformed_entry_skipped(self):
        data = {
            "errors": [
                ["not", "a", "dict"],                     # malformed
                {"code": "NAME_UNKNOWN", "message": "ok"},  # valid
            ]
        }
        r = OciErrorResponse.from_dict(data)
        assert len(r.errors) == 1
        assert r.errors[0].code == "NAME_UNKNOWN"

    def test_all_malformed_entries_gives_empty(self):
        data = {"errors": [123, None, "oops"]}
        r = OciErrorResponse.from_dict(data)
        assert r.errors == []


class TestOciErrorResponseFromJson:

    def test_parses_single_error(self):
        r = OciErrorResponse.from_json(_SINGLE_ERROR_JSON)
        assert r.errors[0].code == "MANIFEST_UNKNOWN"
        assert r.errors[0].message == "manifest unknown"
        assert r.errors[0].detail == {"Tag": "v1.0"}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            OciErrorResponse.from_json("{not valid json}")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            OciErrorResponse.from_json("")


class TestOciErrorResponseFirstDetail:

    def test_returns_formatted_first_error(self):
        r = OciErrorResponse.from_json(_SINGLE_ERROR_JSON)
        assert r.first_detail() == "MANIFEST_UNKNOWN: manifest unknown"

    def test_returns_first_of_multiple(self):
        r = OciErrorResponse.from_json(_MULTI_ERROR_JSON)
        assert r.first_detail() == "UNAUTHORIZED: authentication required"

    def test_empty_errors_returns_empty_string(self):
        r = OciErrorResponse(errors=[])
        assert r.first_detail() == ""


class TestOciErrorResponseSerialization:

    def test_round_trip(self):
        r = OciErrorResponse.from_json(_SINGLE_ERROR_JSON)
        d = r.to_dict()
        assert d["errors"][0]["code"] == "MANIFEST_UNKNOWN"
        assert d["errors"][0]["detail"] == {"Tag": "v1.0"}

    def test_to_json_is_valid_json(self):
        r = OciErrorResponse.from_json(_MULTI_ERROR_JSON)
        parsed = json.loads(r.to_json())
        assert len(parsed["errors"]) == 2

    def test_to_json_omits_null_detail(self):
        r = OciErrorResponse(errors=[OciErrorDetail(code="X", message="y")])
        parsed = json.loads(r.to_json())
        assert "detail" not in parsed["errors"][0]

    def test_empty_response_serializes(self):
        r = OciErrorResponse()
        assert r.to_dict() == {"errors": []}


# ===========================================================================
# TestOciErrorResponseFromResponse — never-raises contract
# ===========================================================================

class TestOciErrorResponseFromResponse:

    def test_single_error(self):
        r = OciErrorResponse.from_response(_make_response(_SINGLE_ERROR_JSON, 404))
        assert r.first_detail() == "MANIFEST_UNKNOWN: manifest unknown"

    def test_empty_body_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response("", 500))
        assert r.errors == []
        assert r.first_detail() == ""

    def test_whitespace_only_body_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response("   \n", 500))
        assert r.errors == []

    def test_invalid_json_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response("{bad json}", 400))
        assert r.errors == []

    def test_non_dict_json_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response("[1, 2, 3]", 400))
        assert r.errors == []

    def test_non_oci_json_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response(_MISSING_ERRORS_JSON, 404))
        assert r.errors == []
        assert r.first_detail() == ""

    def test_null_errors_returns_empty(self):
        r = OciErrorResponse.from_response(_make_response(_NULL_ERRORS_JSON, 500))
        assert r.errors == []

    def test_does_not_raise_on_exception(self):
        resp = MagicMock()
        resp.text = MagicMock(side_effect=RuntimeError("boom"))
        # Must not raise
        r = OciErrorResponse.from_response(resp)
        assert r.errors == []
