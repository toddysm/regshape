#!/usr/bin/env python3

"""Tests for :mod:`regshape.cli.formatting`."""

import json

import pytest

from regshape.cli.formatting import (
    emit_error,
    emit_json,
    emit_list,
    emit_table,
    emit_text,
    format_key_value,
    format_size,
    progress_status,
    short_digest,
)


# ===========================================================================
# emit_json
# ===========================================================================


class TestEmitJson:
    def test_stdout(self, capsys):
        emit_json({"key": "value", "num": 42})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"key": "value", "num": 42}
        # Verify 2-space indentation
        assert '  "key"' in captured.out

    def test_list(self, capsys):
        emit_json([1, 2, 3])
        captured = capsys.readouterr()
        assert json.loads(captured.out) == [1, 2, 3]

    def test_file_output(self, tmp_path):
        out_file = str(tmp_path / "out.json")
        emit_json({"a": 1}, output_path=out_file)
        content = open(out_file).read()
        assert json.loads(content) == {"a": 1}
        assert content.endswith("\n")

    def test_err_goes_to_stderr(self, capsys):
        emit_json({"error": "fail"}, err=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert json.loads(captured.err) == {"error": "fail"}


# ===========================================================================
# emit_text
# ===========================================================================


class TestEmitText:
    def test_stdout(self, capsys):
        emit_text("hello world")
        assert capsys.readouterr().out == "hello world\n"

    def test_file_output(self, tmp_path):
        out_file = str(tmp_path / "out.txt")
        emit_text("hello", output_path=out_file)
        assert open(out_file).read() == "hello\n"

    def test_file_output_preserves_trailing_newline(self, tmp_path):
        out_file = str(tmp_path / "out.txt")
        emit_text("hello\n", output_path=out_file)
        assert open(out_file).read() == "hello\n"

    def test_err_goes_to_stderr(self, capsys):
        emit_text("oops", err=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "oops" in captured.err


# ===========================================================================
# emit_error
# ===========================================================================


class TestEmitError:
    def test_format_and_exit(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            emit_error("registry/repo:tag", "manifest not found")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err == "Error [registry/repo:tag]: manifest not found\n"
        assert captured.out == ""

    def test_custom_exit_code(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            emit_error("ref", "bad input", exit_code=2)
        assert exc_info.value.code == 2

    def test_output_goes_to_stderr(self, capsys):
        with pytest.raises(SystemExit):
            emit_error("ctx", "reason")
        captured = capsys.readouterr()
        assert "Error [ctx]: reason" in captured.err
        assert captured.out == ""


# ===========================================================================
# emit_table
# ===========================================================================


class TestEmitTable:
    def test_with_headers(self, capsys):
        emit_table(
            [["sha256:abc", "sbom", "1024"], ["sha256:def", "sig", "2048"]],
            headers=["DIGEST", "TYPE", "SIZE"],
        )
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        assert len(lines) == 3
        assert "DIGEST" in lines[0]
        assert "sha256:abc" in lines[1]
        assert "sha256:def" in lines[2]

    def test_without_headers(self, capsys):
        emit_table([["a", "bb"], ["ccc", "d"]])
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        assert len(lines) == 2

    def test_column_alignment(self, capsys):
        emit_table(
            [["short", "x"], ["longer_value", "y"]],
            headers=["COL1", "COL2"],
        )
        out = capsys.readouterr().out
        lines = out.strip().split("\n")
        # All values in col2 should start at the same column
        second_col_values = ["COL2", "x", "y"]
        col2_positions = [
            line.index(value) for line, value in zip(lines, second_col_values)
        ]
        assert len(set(col2_positions)) == 1
        assert len(lines) == 3

    def test_empty_rows(self, capsys):
        emit_table([])
        assert capsys.readouterr().out == ""


# ===========================================================================
# emit_list
# ===========================================================================


class TestEmitList:
    def test_stdout(self, capsys):
        emit_list(["tag1", "tag2", "tag3"])
        assert capsys.readouterr().out == "tag1\ntag2\ntag3\n"

    def test_file_output(self, tmp_path):
        out_file = str(tmp_path / "tags.txt")
        emit_list(["a", "b"], output_path=out_file)
        assert open(out_file).read() == "a\nb\n"


# ===========================================================================
# Docker table formatting
# ===========================================================================


class TestFormatSize:
    """Preserve Docker's compact binary size units and rounding."""

    @pytest.mark.parametrize(("size_bytes", "expected"), [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1KB"),
        (1536, "2KB"),
        (2560, "2KB"),
        (1_048_575, "1024KB"),
        (1_048_576, "1MB"),
        (1_572_864, "2MB"),
        (1_073_741_823, "1024MB"),
        (1_073_741_824, "1.00GB"),
        (1_342_177_280, "1.25GB"),
        (1_099_511_627_776, "1024.00GB"),
    ])
    def test_compact_size(self, size_bytes: int, expected: str) -> None:
        """Keep unit transitions, ties-to-even rounding and GB precision."""
        assert format_size(size_bytes) == expected


class TestShortDigest:
    """Preserve Docker image ID abbreviation without validating digests."""

    @pytest.mark.parametrize(("digest", "expected"), [
        ("sha256:0123456789abcdef", "0123456789ab"),
        ("sha256:abc", "abc"),
        ("sha256:", ""),
        ("", ""),
        ("abc", "abc"),
        ("0123456789abcdef0123456789", "0123456789abcdef012"),
        ("sha512:0123456789abcdef", "sha512:0123456789ab"),
    ])
    def test_image_id(self, digest: str, expected: str) -> None:
        """Strip only SHA-256's prefix after the legacy 19-character limit."""
        assert short_digest(digest) == expected


# ===========================================================================
# format_key_value
# ===========================================================================


class TestFormatKeyValue:
    def test_basic_alignment(self):
        result = format_key_value([
            ("Digest", "sha256:abc"),
            ("Media Type", "application/vnd.oci.image.manifest.v1+json"),
            ("Size", "1234"),
        ])
        lines = result.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("Digest    :")
        assert lines[1].startswith("Media Type:")
        assert lines[2].startswith("Size      :")

    def test_custom_separator(self):
        result = format_key_value([("Key", "val")], separator="=")
        assert result == "Key= val"

    def test_empty(self):
        assert format_key_value([]) == ""


# ===========================================================================
# progress_status
# ===========================================================================


class TestProgressStatus:
    def test_output_to_stderr(self, capsys):
        progress_status("Uploading blob...")
        captured = capsys.readouterr()
        assert "Uploading blob..." in captured.err
        assert captured.out == ""
