"""Tests for pdf module."""

import os
from unittest.mock import MagicMock, patch

import pytest

from corderohq.archiver.pdf import convert_to_pdfa


def _successful_run() -> MagicMock:
    result = MagicMock()
    result.returncode = 0
    return result


@patch("corderohq.archiver.pdf.subprocess.run", return_value=_successful_run())
def test_pdf_input_skips_pillow(mock_run: MagicMock, tmp_path: MagicMock) -> None:
    output_pdf = b"converted-pdf-output"

    with (
        patch("corderohq.archiver.pdf.Image") as mock_image_mod,
        patch("pathlib.Path.read_bytes", return_value=output_pdf),
        patch("pathlib.Path.write_bytes"),
    ):
        result = convert_to_pdfa(b"pdf-input", "application/pdf")

    mock_image_mod.open.assert_not_called()
    mock_run.assert_called_once()
    assert result == output_pdf


@patch("corderohq.archiver.pdf.subprocess.run", return_value=_successful_run())
@patch("corderohq.archiver.pdf.Image")
def test_jpeg_input_uses_pillow_then_ghostscript(mock_image_mod: MagicMock, mock_run: MagicMock) -> None:
    mock_img = MagicMock()
    mock_image_mod.open.return_value = mock_img
    output_pdf = b"converted-pdf-output"

    with (
        patch("pathlib.Path.read_bytes", return_value=output_pdf),
        patch("pathlib.Path.write_bytes"),
    ):
        result = convert_to_pdfa(b"jpeg-data", "image/jpeg")

    mock_image_mod.open.assert_called_once()
    mock_img.save.assert_called_once()
    save_args = mock_img.save.call_args
    assert save_args[0][1] == "PDF"
    mock_run.assert_called_once()
    assert result == output_pdf


@patch("corderohq.archiver.pdf.subprocess.run", return_value=_successful_run())
def test_ghostscript_called_with_correct_args(mock_run: MagicMock) -> None:
    with (
        patch("pathlib.Path.read_bytes", return_value=b"output"),
        patch("pathlib.Path.write_bytes"),
    ):
        convert_to_pdfa(b"pdf-input", "application/pdf")

    gs_args = mock_run.call_args[0][0]
    assert "-dPDFA=2" in gs_args
    assert "-dBATCH" in gs_args
    assert "-dNOPAUSE" in gs_args
    assert "-sDEVICE=pdfwrite" in gs_args
    assert mock_run.call_args[1]["capture_output"] is True


@patch("corderohq.archiver.pdf.subprocess.run", return_value=_successful_run())
def test_ghostscript_library_path_stays_in_the_subprocess(mock_run: MagicMock) -> None:
    # The Lambda process must keep the runtime OpenSSL. Only Ghostscript gets the
    # bundled libraries, so the Python `ssl` module still imports.
    with (
        patch("pathlib.Path.read_bytes", return_value=b"output"),
        patch("pathlib.Path.write_bytes"),
    ):
        convert_to_pdfa(b"pdf-input", "application/pdf")

    gs_env = mock_run.call_args[1]["env"]
    assert gs_env["LD_LIBRARY_PATH"] == "/var/task/lib"
    assert gs_env["GS_LIB"] == "/var/task/share/Resource/Init:/var/task/share/lib"
    assert gs_env["PATH"] == os.environ["PATH"]


@patch("corderohq.archiver.pdf.subprocess.run", return_value=_successful_run())
@patch("corderohq.archiver.pdf.Image")
def test_png_input_uses_pillow(mock_image_mod: MagicMock, mock_run: MagicMock) -> None:
    mock_image_mod.open.return_value = MagicMock()

    with (
        patch("pathlib.Path.read_bytes", return_value=b"output"),
        patch("pathlib.Path.write_bytes"),
    ):
        convert_to_pdfa(b"png-data", "image/png")

    mock_image_mod.open.assert_called_once()


@patch("corderohq.archiver.pdf.subprocess.run")
def test_ghostscript_failure_raises_with_stderr(mock_run: MagicMock) -> None:
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = b""
    mock_result.stderr = b"Error: something went wrong"
    mock_run.return_value = mock_result

    with (
        patch("pathlib.Path.write_bytes"),
        pytest.raises(RuntimeError, match="something went wrong"),
    ):
        convert_to_pdfa(b"pdf-input", "application/pdf")
